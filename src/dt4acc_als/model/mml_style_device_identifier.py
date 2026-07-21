from dataclasses import dataclass


@dataclass(frozen=True)
class MMLStyleDeviceIdentifier:
    family: str
    sector: int
    child: int

    def mml_device_index(self):
        """matching how a device reference is stored in ao"""
        return (self.sector, self.child)

    def as_abbreviation(self) -> str:
        return f"{self.family}-sec_{self.sector}-child_{self.child}"
