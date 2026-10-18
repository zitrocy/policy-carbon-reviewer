"""Carbon intensity data access and region comparison.

Data: Google's official 2025 annual CFE% and grid carbon intensity per region,
bundled at build time (app/data/carbon_regions.json). Values are grid metrics,
not workload emission measurements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "carbon_regions.json"


@dataclass(frozen=True)
class RegionCarbon:
    region: str
    location: str
    country: str
    cfe_pct: float
    grid_carbon_intensity: float

    @property
    def low_carbon(self) -> bool:
        """Badge for a genuinely clean grid.

        Grid carbon intensity is the ranking metric everywhere in this app, so
        the badge follows it. A high CFE% alone does not earn the badge: CFE is
        an annual *matching* figure and can be high in a region whose grid is
        still carbon-heavy hour to hour (us-central1 is 88% CFE on a
        432 gCO2eq/kWh grid). Badging that region "low carbon" next to a table
        that ranks it near the bottom would contradict the table.

        CFE% is still shown as its own column, which is where it belongs.
        """
        if self.grid_carbon_intensity is None:
            return self.cfe_pct >= 75
        return self.grid_carbon_intensity <= 200


@dataclass
class ComparisonRow:
    region: str
    location: str
    cfe_pct: float
    grid_carbon_intensity: float
    low_carbon: bool
    delta_pct_vs_baseline: float | None  # negative = cleaner than baseline


class CarbonCatalog:
    def __init__(self, path: Path = DATA_PATH):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.metadata: dict = raw["metadata"]
        self._regions: dict[str, RegionCarbon] = {
            r["region"]: RegionCarbon(
                region=r["region"],
                location=r["location"],
                country=r["country"],
                cfe_pct=float(r["cfe_pct"]),
                grid_carbon_intensity=float(r["grid_carbon_intensity"]),
            )
            for r in raw["regions"]
        }

    def get(self, region: str) -> RegionCarbon | None:
        return self._regions.get(region)

    def known(self, region: str) -> bool:
        return region in self._regions

    def all(self) -> list[RegionCarbon]:
        """Every region in the catalog. Used to give the policy interpreter a
        closed set to choose from, so it cannot invent a region ID."""
        return list(self._regions.values())

    def compare(self, regions: list[str], baseline: str | None) -> list[ComparisonRow]:
        """Compare known regions by grid carbon intensity, cleanest first.

        Unknown regions are skipped here; the caller reports them separately.
        """
        baseline_value = None
        if baseline and baseline in self._regions:
            baseline_value = self._regions[baseline].grid_carbon_intensity

        rows = []
        for region in regions:
            meta = self._regions.get(region)
            if meta is None:
                continue
            delta = None
            if baseline_value and baseline_value > 0:
                delta = round(
                    (meta.grid_carbon_intensity - baseline_value) / baseline_value * 100, 1
                )
            rows.append(
                ComparisonRow(
                    region=meta.region,
                    location=meta.location,
                    cfe_pct=meta.cfe_pct,
                    grid_carbon_intensity=meta.grid_carbon_intensity,
                    low_carbon=meta.low_carbon,
                    delta_pct_vs_baseline=delta,
                )
            )
        rows.sort(key=lambda r: r.grid_carbon_intensity)
        return rows
