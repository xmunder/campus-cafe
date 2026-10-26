from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class CreateOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    productId: StrictStr = Field(min_length=1, max_length=64)
    quantity: StrictInt = Field(ge=1, le=10)


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CreateOrderItem] = Field(min_length=1, max_length=10)
