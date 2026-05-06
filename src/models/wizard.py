from pydantic import BaseModel


class WizardResponse(BaseModel):
    name: str
    house: str
    species: str
    wizard: bool
    powerScore: int
