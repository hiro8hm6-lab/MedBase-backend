from typing import Annotated

from fastapi import Depends

from medbase.config import Settings, get_settings


def current_user(settings: Annotated[Settings, Depends(get_settings)]) -> str:
    """Replace this dependency with verified session identity before public deployment.

    Client-supplied owner IDs/headers are intentionally never trusted.
    """
    return settings.dev_user_id
