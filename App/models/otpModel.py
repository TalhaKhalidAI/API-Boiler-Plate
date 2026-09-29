from pydantic import BaseModel

class SendOTPRequest(BaseModel):
    username: str   