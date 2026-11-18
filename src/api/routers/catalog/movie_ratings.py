from fastapi import APIRouter, Depends, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.ratings import (
    MovieRatingResource,
    MovieRatingUpsertRequest,
    RatingDimensionResource,
    RatingDimensionUpsertRequest,
)
from src.service.catalog import RatingService

dimensions_router = APIRouter(
    prefix="/movie-rating-dimensions",
    tags=["movie-ratings"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)

ratings_router = APIRouter(
    prefix="/movies",
    tags=["movie-ratings"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@dimensions_router.get("", response_model=list[RatingDimensionResource])
def list_rating_dimensions():
    return RatingService.list_dimensions()


@dimensions_router.post(
    "",
    response_model=RatingDimensionResource,
    status_code=status.HTTP_201_CREATED,
)
def create_rating_dimension(payload: RatingDimensionUpsertRequest):
    return RatingService.create_dimension(payload)


@dimensions_router.put("/{dimension_id}", response_model=RatingDimensionResource)
def update_rating_dimension(
    dimension_id: int, payload: RatingDimensionUpsertRequest
):
    return RatingService.update_dimension(dimension_id, payload)


@dimensions_router.delete("/{dimension_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rating_dimension(dimension_id: int):
    RatingService.delete_dimension(dimension_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@ratings_router.get("/{movie_number}/rating", response_model=MovieRatingResource)
def get_movie_rating(movie_number: str):
    return RatingService.get_rating(movie_number)


@ratings_router.put("/{movie_number}/rating", response_model=MovieRatingResource)
def save_movie_rating(movie_number: str, payload: MovieRatingUpsertRequest):
    return RatingService.save_rating(movie_number, payload)


@ratings_router.delete(
    "/{movie_number}/rating", status_code=status.HTTP_204_NO_CONTENT
)
def delete_movie_rating(movie_number: str):
    RatingService.delete_rating(movie_number)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
