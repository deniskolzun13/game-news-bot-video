"""Загрузка видео на Google Drive через официальный API.

OAuth: при первом запуске открывается браузер, вы даёте доступ,
refresh token сохраняется локально в token.json. Пароль Google не хранится.

Google-библиотеки импортируются лениво: при UPLOAD_TO_DRIVE=false бот
работает без установки google-api-python-client.
"""

import logging
from pathlib import Path

logger = logging.getLogger("drive")

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    pass


class DriveUploader:
    def __init__(self, credentials_file: Path, token_file: Path,
                 folder_id: str = "") -> None:
        self.credentials_file = Path(credentials_file)
        self.token_file = Path(token_file)
        self.default_folder_id = folder_id
        self.service = None

    # --- Аутентификация ---
    def is_configured(self) -> bool:
        return self.credentials_file.exists()

    def authenticate(self) -> None:
        """Получает/обновляет токен. При первом запуске — через браузер."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = None
        if self.token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_file), _SCOPES
                )
            except (ValueError, OSError):
                creds = None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            if not self.credentials_file.exists():
                raise DriveError(
                    "Не найден файл credentials.json.\n"
                    "Получите его в Google Cloud Console (Desktop app OAuth client)\n"
                    "и положите в корень проекта. Подробности — в README."
                )
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_file), _SCOPES
            )
            creds = flow.run_local_server(port=0, prompt="consent")
        self.token_file.write_text(creds.to_json(), encoding="utf-8")
        self.service = build("drive", "v3", credentials=creds)

    def _find_folder_by_name(self, name: str, parent_id: str = "") -> str | None:
        query = (
            f"name = '{name}' and mimeType = '{_DRIVE_FOLDER_MIME}'"
            " and trashed = false"
        )
        if parent_id:
            query += f" and '{parent_id}' in parents"
        result = self.service.files().list(
            q=query, fields="files(id)", pageSize=1
        ).execute()
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent_id: str = "") -> str:
        body = {"name": name, "mimeType": _DRIVE_FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        folder = self.service.files().create(body=body, fields="id").execute()
        return folder["id"]

    def ensure_root_folder(self) -> str:
        """Возвращает ID корневой папки GAMENEWS_VIDEOS."""
        if self.default_folder_id:
            return self.default_folder_id
        name = "GAMENEWS_VIDEOS"
        folder_id = self._find_folder_by_name(name)
        if not folder_id:
            folder_id = self._create_folder(name)
            logger.info("[DRIVE] Создана папка на Drive: %s", name)
        return folder_id

    def ensure_dated_folder(self, root_id: str, date_str: str) -> str:
        """Возвращает ID подпапки вида 2026-08-20 внутри root_id."""
        folder_id = self._find_folder_by_name(date_str, root_id)
        if not folder_id:
            folder_id = self._create_folder(date_str, root_id)
        return folder_id

    # --- Загрузка ---
    def upload(self, local_path: Path, folder_id: str) -> str:
        """Загружает файл в папку. Возвращает file_id."""
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        if self.service is None:
            raise DriveError("Аутентификация не выполнена")
        local_path = Path(local_path)
        media = MediaFileUpload(
            str(local_path), mimetype="video/mp4", resumable=True
        )
        body = {
            "name": local_path.name,
            "parents": [folder_id],
            "mimeType": "video/mp4",
        }
        try:
            file = self.service.files().create(
                body=body, media_body=media, fields="id"
            ).execute()
        except HttpError as exc:
            raise DriveError(f"Ошибка загрузки на Google Drive: {exc}") from exc
        logger.info("[DRIVE] OK (file id %s)", file.get("id"))
        return file["id"]

    def upload_auto(self, local_path: Path, date_str: str) -> str:
        """Короткий путь: корень → папка даты → загрузка файла."""
        root = self.ensure_root_folder()
        dated = self.ensure_dated_folder(root, date_str)
        return self.upload(local_path, dated)

    def make_shareable(self, file_id: str) -> str:
        """Делает файл доступным по ссылке и возвращает URL."""
        self.service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
        return f"https://drive.google.com/file/d/{file_id}/view"


__all__ = ["DriveUploader", "DriveError"]