from dataclasses import dataclass


@dataclass
class ExecutionErrorHistoryItem:
    code: str
    e: Exception
