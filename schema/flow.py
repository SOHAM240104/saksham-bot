from pydantic import BaseModel, Field


class NameInput(BaseModel):
    name: str = Field(..., min_length=1)


class NameOutput(BaseModel):
    name: str


class TrainURLInput(BaseModel):
    url: str = Field(..., min_length=1)


class TrainBulkURLsInput(BaseModel):
    urls: list[str] = Field(..., min_length=1)
