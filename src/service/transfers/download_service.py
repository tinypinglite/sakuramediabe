from src.service.transfers.download_client_service import DownloadClientService
from src.service.transfers.download_task_service import DownloadTaskService


class DownloadService:
    list_clients = DownloadClientService.list_clients
    create_client = DownloadClientService.create_client
    update_client = DownloadClientService.update_client
    delete_client = DownloadClientService.delete_client
    list_tasks = DownloadTaskService.list_tasks
    delete_tasks = DownloadTaskService.delete_tasks
