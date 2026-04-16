from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Threshold:
    name: str              # e.g. "ram_warn"
    plugin: str            # plugin name that owns this threshold
    metric_key: str        # key in the plugin's collect() dict
    level: str             # "warn" | "critical"
    value: float           # threshold value
    direction: str = "above"  # "above": fire when metric >= value (CPU, RAM)
                               # "below": fire when metric <= value (battery charge)


class BaseCollector(ABC):
    name: str   # unique plugin identifier, matches config.yaml plugins key
    interval: int  # poll interval in seconds

    @abstractmethod
    async def collect(self) -> dict: ...

    @abstractmethod
    def thresholds(self) -> list[Threshold]: ...
