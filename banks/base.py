"""Statement parser interface and registry."""
from abc import ABC, abstractmethod
from openpyxl.workbook import Workbook
from core.models import ParsedStatement


class BankParser(ABC):
    bank_code: str

    @abstractmethod
    def can_parse(self, wb: Workbook) -> bool:
        """Identify supported statement headers."""
        raise NotImplementedError

    @abstractmethod
    def parse(self, wb: Workbook) -> ParsedStatement:
        """Read statement data and validation warnings."""
        raise NotImplementedError

    @abstractmethod
    def clean_merchant(self, raw_description: str) -> str:
        """Normalize a transaction description."""
        raise NotImplementedError

    @abstractmethod
    def is_fee(self, raw_description: str) -> bool:
        """Identify bank fee descriptions."""
        raise NotImplementedError


REGISTRY: list[BankParser] = []


def detect_parser(wb: Workbook) -> BankParser | None:
    """Return the first parser recognizing the workbook."""
    return next((parser for parser in REGISTRY if parser.can_parse(wb)), None)
