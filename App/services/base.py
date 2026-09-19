# App/services/base.py

from sqlalchemy.ext.asyncio import AsyncSession
from App.core.LoggingInit import core_logger


class BaseService:
    """
    Base class for all services.

    Rules:
      - Services NEVER commit. The request-scoped session (from
        get_db()) commits at the end of the request.
      - Services NEVER instantiate their own session. They receive one.
      - Services raise domain errors / PermissionError, not HTTPException.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session