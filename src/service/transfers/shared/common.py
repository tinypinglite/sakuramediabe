"""Expressions shared by catalogue subscription state and transfers."""

from __future__ import annotations

from peewee import fn

from src.common.media_import_status import UNFINISHED_IMPORT_STATUSES
from src.model import DownloadTask, Movie


def _download_task_movie_match_expression():
    return DownloadTask.movie == Movie.movie_number


def active_download_task_exists_expression():
    active_tasks = DownloadTask.select(DownloadTask.id).where(
        _download_task_movie_match_expression()
        & DownloadTask.state.in_(("queued", "downloading", "completed"))
    )
    return fn.EXISTS(active_tasks)


def unfinished_import_download_task_exists_expression():
    tasks = DownloadTask.select(DownloadTask.id).where(
        _download_task_movie_match_expression()
        & DownloadTask.state.in_(("queued", "downloading", "completed"))
        & DownloadTask.import_status.in_(UNFINISHED_IMPORT_STATUSES)
    )
    return fn.EXISTS(tasks)
