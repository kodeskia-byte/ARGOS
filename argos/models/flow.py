from typing import List, Optional, Union
from enum import Enum
from pydantic import BaseModel, Field

class ActionType(str, Enum):
    OPEN_URL = "open_url"
    CLICK = "click"
    INPUT = "input"
    ASSERT = "assert"
    WAIT = "wait"

class Step(BaseModel):
    action: ActionType
    xpath: Optional[str] = None
    selector: Optional[str] = None  # Generic selector if xpath is not preferred
    value: Optional[str] = None
    description: Optional[str] = None
    timeout: Optional[int] = 30000

class Flow(BaseModel):
    name: str = "Anonymous Flow"
    description: Optional[str] = None
    steps: List[Step]
