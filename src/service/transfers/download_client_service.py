from typing import List

from src.model import DownloadClient, DownloadTask
from src.schema.transfers.downloads import (
    DownloadClientCreateRequest,
    DownloadClientResource,
    DownloadClientUpdateRequest,
)
from src.service.transfers.common import (
    ensure_name_available,
    require_client,
    require_media_library,
    validate_absolute_path,
    validate_base_url,
    validate_media_library_id,
    validate_non_empty,
)


class DownloadClientService:
    @classmethod
    def list_clients(cls) -> List[DownloadClientResource]:
        clients = list(
            DownloadClient.select().order_by(
                DownloadClient.created_at.desc(),
                DownloadClient.id.desc(),
            )
        )
        return DownloadClientResource.from_models(clients)

    @classmethod
    def create_client(cls, payload: DownloadClientCreateRequest) -> DownloadClientResource:
        name = validate_non_empty(
            payload.name,
            "invalid_download_client_name",
            "Download client name cannot be empty",
        )
        username = validate_non_empty(
            payload.username,
            "invalid_download_client_username",
            "Download client username cannot be empty",
        )
        password = validate_non_empty(
            payload.password,
            "invalid_download_client_password",
            "Download client password cannot be empty",
        )
        media_library_id = validate_media_library_id(payload.media_library_id)
        require_media_library(media_library_id)
        ensure_name_available(name)

        client = DownloadClient.create(
            name=name,
            base_url=validate_base_url(payload.base_url),
            username=username,
            password=password,
            client_save_path=validate_absolute_path(
                payload.client_save_path,
                field_name="client_save_path",
            ),
            local_root_path=validate_absolute_path(
                payload.local_root_path,
                field_name="local_root_path",
            ),
            media_library=media_library_id,
        )
        return DownloadClientResource.from_model(client)

    @classmethod
    def update_client(
        cls,
        client_id: int,
        payload: DownloadClientUpdateRequest,
    ) -> DownloadClientResource:
        client = require_client(client_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            from src.api.exception.errors import ApiError

            raise ApiError(
                422,
                "empty_download_client_update",
                "At least one field must be provided",
            )

        if "name" in update_data:
            name = validate_non_empty(
                update_data["name"],
                "invalid_download_client_name",
                "Download client name cannot be empty",
            )
            if name != client.name:
                ensure_name_available(name, exclude_client_id=client.id)
            client.name = name

        if "base_url" in update_data:
            client.base_url = validate_base_url(update_data["base_url"])

        if "username" in update_data:
            client.username = validate_non_empty(
                update_data["username"],
                "invalid_download_client_username",
                "Download client username cannot be empty",
            )

        if "password" in update_data:
            client.password = validate_non_empty(
                update_data["password"],
                "invalid_download_client_password",
                "Download client password cannot be empty",
            )

        if "client_save_path" in update_data:
            client.client_save_path = validate_absolute_path(
                update_data["client_save_path"],
                field_name="client_save_path",
            )

        if "local_root_path" in update_data:
            client.local_root_path = validate_absolute_path(
                update_data["local_root_path"],
                field_name="local_root_path",
            )

        if "media_library_id" in update_data:
            media_library_id = validate_media_library_id(update_data["media_library_id"])
            require_media_library(media_library_id)
            client.media_library = media_library_id

        client.save()
        return DownloadClientResource.from_model(client)

    @classmethod
    def delete_client(cls, client_id: int) -> None:
        from src.api.exception.errors import ApiError

        client = require_client(client_id)
        if DownloadTask.select().where(DownloadTask.client == client.id).exists():
            raise ApiError(
                409,
                "download_client_in_use",
                "Download client is still referenced by download tasks",
                {"client_id": client.id},
            )
        client.delete_instance()
