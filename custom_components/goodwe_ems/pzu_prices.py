"""Prețuri PZU (Piața pentru Ziua Următoare) pentru dispecerizarea bateriei.

Sub SDAC, PZU a trecut la MTU de 15 minute: OPCOM publică ROPEX_DAM_15min cu
96 de valori pe zi de livrare (92 sau 100 în zilele de schimbare a orei).
„Prețul orar" afișat în card este media aritmetică a patru sferturi de oră.

Sursa primară e OPCOM (lei/MWh, prin scraping HTML), rezerva e ENTSO-E
(EUR/MWh, prin API XML). Pentru dispecerizare contează doar ordinea relativă
intra-zi, deci nu se face conversie valutară; cursul afectează doar afișarea.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup

_LOGGER = logging.getLogger(__name__)

RO_TZ = ZoneInfo("Europe/Bucharest")

OPCOM_HOME = "https://www.opcom.ro/pp/index.php"
ENTSOE_API = "https://web-api.tp.entsoe.eu/api"
ENTSOE_DOMAIN = "10YRO-TEL------P"

USER_AGENT = "HomeAssistant-goodwe_ems/1.0"

# Limite de plauzibilitate. PZU poate fi negativ (surplus fotovoltaic) și poate
# depăși 5000 lei/MWh în criză, dar în afara acestor margini e sigur o eroare
# de parsare, nu un preț real.
PRICE_MIN = -2000.0
PRICE_MAX = 10000.0

# O serie mai veche de atât nu mai are voie să comande invertorul.
MAX_AGE_HOURS = 18

MTU_PER_HOUR = 4
VALID_MTU_COUNTS = (92, 96, 100)  # zi scurtă / normală / lungă (DST)


class PriceError(Exception):
    """Prețurile nu au putut fi obținute sau nu trec validarea."""


def parse_ro_number(text: str) -> float | None:
    """Convertește „1.234,56" în 1234.56."""
    if not text:
        return None
    cleaned = text.strip().replace("\xa0", "").replace(" ", "")
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


UTC = ZoneInfo("UTC")


def expected_mtu_count(day: date) -> int:
    """Numărul de intervale de 15 minute din ziua respectivă, cu DST.

    Conversia la UTC nu e ornamentală: scăderea a două datetime-uri care
    partajează același obiect tzinfo ignoră offset-ul și dă întotdeauna 24 de
    ore, deci 96 de intervale și în zilele de schimbare a orei.
    """
    nxt = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=RO_TZ).astimezone(UTC)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=RO_TZ).astimezone(UTC)
    return int((end - start).total_seconds() // 900)


# --------------------------------------------------------------------------
# Seria de prețuri
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceSeries:
    """Prețurile unei zile de livrare. Imutabilă și validată la construcție."""

    day: date
    values: tuple[float, ...]
    currency: str
    source: str
    fetched_at: datetime

    def __post_init__(self) -> None:
        if len(self.values) not in VALID_MTU_COUNTS:
            raise PriceError(
                f"{self.source}: {len(self.values)} intervale, se așteptau "
                f"{expected_mtu_count(self.day)}"
            )
        if len(self.values) != expected_mtu_count(self.day):
            raise PriceError(
                f"{self.source}: {len(self.values)} intervale nu corespund zilei {self.day}"
            )
        for value in self.values:
            if not PRICE_MIN <= value <= PRICE_MAX:
                raise PriceError(f"{self.source}: preț implauzibil {value}")

    # -- acces --------------------------------------------------------------

    def start_utc(self) -> datetime:
        """Miezul nopții local al zilei de livrare, exprimat în UTC."""
        return datetime(self.day.year, self.day.month, self.day.day, tzinfo=RO_TZ).astimezone(UTC)

    def index_at(self, moment: datetime | None = None) -> int:
        """Indexul MTU corespunzător momentului dat (implicit: acum)."""
        moment = (moment or datetime.now(RO_TZ)).astimezone(UTC)
        idx = int((moment - self.start_utc()).total_seconds() // 900)
        return max(0, min(len(self.values) - 1, idx))

    def local_time_of(self, index: int) -> datetime:
        """Ora locală de început a intervalului, corectă și la schimbarea orei."""
        return (self.start_utc() + timedelta(minutes=15 * index)).astimezone(RO_TZ)

    def value_at(self, moment: datetime | None = None) -> float:
        return self.values[self.index_at(moment)]

    def hourly_averages(self) -> list[float]:
        """Media pe oră a sferturilor de oră — ce afișează cardul."""
        out: list[float] = []
        for i in range(0, len(self.values), MTU_PER_HOUR):
            chunk = self.values[i : i + MTU_PER_HOUR]
            out.append(sum(chunk) / len(chunk))
        return out

    @property
    def average(self) -> float:
        return sum(self.values) / len(self.values)

    @property
    def minimum(self) -> float:
        return min(self.values)

    @property
    def maximum(self) -> float:
        return max(self.values)

    # -- gardul de siguranță ------------------------------------------------

    def is_actionable(self, now: datetime | None = None) -> bool:
        """Seria are voie să comande invertorul?

        Două condiții simultane: să fie seria zilei curente și să fi fost
        descărcată în ultimele 18 ore. Fără gardul ăsta, un OPCOM căzut la 2
        noaptea înseamnă că la 13:00 bateria se descarcă după programul de ieri.
        """
        now = (now or datetime.now(RO_TZ)).astimezone(RO_TZ)
        if self.day != now.date():
            return False
        return (now - self.fetched_at) < timedelta(hours=MAX_AGE_HOURS)


# --------------------------------------------------------------------------
# Sursa primară: OPCOM
# --------------------------------------------------------------------------


class OpcomSource:
    """Citește tabelul ROPEX_DAM_15min de pe pagina principală OPCOM.

    Fragil prin natura lui: e HTML scraping, nu API. Orice schimbare de layout
    ridică `PriceError`, ceea ce declanșează rezerva ENTSO-E — niciodată o
    întoarcere silențioasă de date parțiale.
    """

    name = "opcom"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def async_fetch(self) -> PriceSeries:
        return self._parse(await self._get(OPCOM_HOME))

    async def _get(self, url: str) -> str:
        async with self._session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            return await resp.text()

    def _parse(self, html: str) -> PriceSeries:
        soup = BeautifulSoup(html, "html.parser")
        table = self._find_table(soup)
        if table is None:
            raise PriceError("opcom: tabelul ROPEX_DAM_15min nu a fost găsit")

        day = self._extract_delivery_day(table)
        values = self._extract_values(table)

        return PriceSeries(
            day=day,
            values=tuple(values),
            currency="RON",
            source=self.name,
            fetched_at=datetime.now(RO_TZ),
        )

    @staticmethod
    def _find_table(soup: BeautifulSoup):
        for table in soup.find_all("table"):
            if "ROPEX_DAM" in table.get_text():
                return table
        return None

    @staticmethod
    def _extract_delivery_day(table) -> date:
        text = table.get_text(" ", strip=True)
        match = re.search(r"[Zz]iua de livrare\s*:?\s*(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text)
        if not match:
            raise PriceError("opcom: ziua de livrare nu a putut fi citită")
        d, m, y = (int(g) for g in match.groups())
        return date(y, m, d)

    @staticmethod
    def _extract_values(table) -> list[float]:
        """Extrage cele 96 de prețuri, sărind agregatele și antetele.

        Euristica: prețurile sunt singurele celule cu zecimale, iar prima celulă
        cu zecimale de pe fiecare rând este agregatul zilnic al indicatorului.
        Rândul cu cele mai multe prețuri este seria de intervale.

        DE VERIFICAT ÎNAINTE DE PRODUCȚIE: euristica a fost construită dintr-o
        randare textuală a paginii, nu din DOM-ul real. Dacă structura diferă,
        e o singură metodă de rescris; `PriceSeries.__post_init__` protejează
        între timp, pentru că orice parsare care nu dă 92/96/100 valori
        plauzibile ridică PriceError și trece pe ENTSO-E.
        """
        best: list[float] = []
        for row in table.find_all("tr"):
            numbers: list[float] = []
            for cell in row.find_all(["td", "th"]):
                text = cell.get_text(strip=True)
                if "," not in text:
                    continue  # întregi = numere de interval, se sar
                value = parse_ro_number(text)
                if value is not None:
                    numbers.append(value)
            if len(numbers) > len(best):
                best = numbers

        if len(best) < min(VALID_MTU_COUNTS):
            raise PriceError(f"opcom: doar {len(best)} prețuri găsite în tabel")

        # Prima valoare a rândului e agregatul zilnic al indicatorului.
        if len(best) - 1 in VALID_MTU_COUNTS:
            best = best[1:]
        return best


# --------------------------------------------------------------------------
# Rezerva: ENTSO-E Transparency Platform
# --------------------------------------------------------------------------


class EntsoeSource:
    """Document A44 (day-ahead prices) pentru zona RO.

    Întoarce EUR/MWh, nu lei/MWh. Pentru dispecerizare nu contează: doar
    ordinea relativă a intervalelor decide când încarci și când descarci.
    """

    name = "entsoe"
    _NS = "{urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3}"

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    async def async_fetch(self, day: date) -> PriceSeries:
        start = datetime(day.year, day.month, day.day, tzinfo=RO_TZ)
        end = start + timedelta(days=1)
        params = {
            "securityToken": self._token,
            "documentType": "A44",
            "in_Domain": ENTSOE_DOMAIN,
            "out_Domain": ENTSOE_DOMAIN,
            "periodStart": start.astimezone(ZoneInfo("UTC")).strftime("%Y%m%d%H00"),
            "periodEnd": end.astimezone(ZoneInfo("UTC")).strftime("%Y%m%d%H00"),
        }
        async with self._session.get(
            ENTSOE_API,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            xml = await resp.text()

        values = self._parse(xml, expected_mtu_count(day))
        return PriceSeries(
            day=day,
            values=tuple(values),
            currency="EUR",
            source=self.name,
            fetched_at=datetime.now(RO_TZ),
        )

    def _parse(self, xml: str, expected: int) -> list[float]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as err:
            raise PriceError(f"entsoe: XML invalid ({err})") from err

        if root.tag.endswith("Acknowledgement_MarketDocument"):
            reason = root.find(f".//{self._NS}text")
            raise PriceError(f"entsoe: {reason.text if reason is not None else 'refuzat'}")

        series: list[float] = []
        for period in root.iter(f"{self._NS}Period"):
            resolution = period.findtext(f"{self._NS}resolution", "")
            # ENTSO-E poate răspunde cu rezoluție orară chiar dacă piața e la
            # 15 minute; fiecare valoare se replică pe cele patru sferturi.
            step = {"PT15M": 1, "PT30M": 2, "PT60M": 4}.get(resolution)
            if step is None:
                raise PriceError(f"entsoe: rezoluție necunoscută {resolution!r}")

            points: dict[int, float] = {}
            for point in period.iter(f"{self._NS}Point"):
                position = point.findtext(f"{self._NS}position")
                amount = point.findtext(f"{self._NS}price.amount")
                if position is None or amount is None:
                    continue
                points[int(position)] = float(amount)

            if not points:
                continue

            # Pozițiile lipsă înseamnă „la fel ca precedenta" în formatul A44.
            last: float | None = None
            for pos in range(1, max(points) + 1):
                last = points.get(pos, last)
                if last is None:
                    raise PriceError("entsoe: serie incompletă, lipsește prima poziție")
                series.extend([last] * step)

        if len(series) != expected:
            raise PriceError(f"entsoe: {len(series)} intervale, se așteptau {expected}")
        return series


# --------------------------------------------------------------------------
# Coordonator cu rezervă
# --------------------------------------------------------------------------


class PzuPriceCoordinator:
    """Menține seria zilei curente, cu OPCOM primar și ENTSO-E ca rezervă."""

    def __init__(
        self, session: aiohttp.ClientSession, entsoe_token: str | None = None
    ) -> None:
        self._primary = OpcomSource(session)
        self._fallback = EntsoeSource(session, entsoe_token) if entsoe_token else None
        self._series: PriceSeries | None = None
        self._monthly: tuple[float, str] | None = None
        self._session = session
        self._lock = asyncio.Lock()

    @property
    def series(self) -> PriceSeries | None:
        return self._series

    @property
    def monthly_weighted(self) -> tuple[float, str] | None:
        return self._monthly

    def actionable_series(self) -> PriceSeries | None:
        """Seria de folosit pentru comenzi, sau None dacă nu e sigură."""
        if self._series is not None and self._series.is_actionable():
            return self._series
        return None

    async def async_refresh(self) -> PriceSeries:
        async with self._lock:
            today = datetime.now(RO_TZ).date()
            errors: list[str] = []

            try:
                series = await self._primary.async_fetch()
            except (PriceError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                errors.append(f"opcom: {err}")
            else:
                if series.day == today:
                    self._series = series
                    return series
                errors.append(f"opcom: ziua publicată este {series.day}, nu {today}")

            if self._fallback is not None:
                try:
                    series = await self._fallback.async_fetch(today)
                except (PriceError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                    errors.append(f"entsoe: {err}")
                else:
                    _LOGGER.warning("PZU: s-a folosit rezerva ENTSO-E (%s)", "; ".join(errors))
                    self._series = series
                    return series

            raise PriceError("Nicio sursă de preț disponibilă -> " + " | ".join(errors))

    async def async_refresh_monthly(self) -> tuple[float, str] | None:
        result = await async_fetch_monthly_weighted_price(self._session)
        if result is not None:
            self._monthly = result
        return self._monthly


# --------------------------------------------------------------------------
# Prețul mediu ponderat lunar (decontare prosumator, Ordin ANRE 15/2022)
# --------------------------------------------------------------------------


async def async_fetch_monthly_weighted_price(
    session: aiohttp.ClientSession,
) -> tuple[float, str] | None:
    """Prețul mediu ponderat PZU al lunii anterioare, în lei/MWh.

    OPCOM îl publică pe la 1 ale lunii, ca anunț text pe pagina principală.
    Aceasta este valoarea de decontare pentru prosumatori — nu media prețurilor
    orare, care dă un rezultat diferit.
    """
    async with session.get(
        OPCOM_HOME,
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        text = BeautifulSoup(await resp.text(), "html.parser").get_text(" ", strip=True)

    match = re.search(
        r"[Pp]re[țt]ul mediu ponderat[^.]{0,80}?pentru\s+(?:luna\s+)?"
        r"([A-Za-zăâîșțĂÂÎȘȚ]+\s+\d{4})[^0-9]{0,40}?([\d.]+,\d+)\s*lei/MWh",
        text,
    )
    if not match:
        _LOGGER.debug("OPCOM: anunțul cu prețul mediu ponderat nu a fost găsit")
        return None

    price = parse_ro_number(match.group(2))
    return (price, match.group(1).strip()) if price is not None else None


# --------------------------------------------------------------------------
# Ajutoare pentru card și decontare
# --------------------------------------------------------------------------


def lei_per_kwh(price_per_mwh: float) -> float:
    """Cardul afișează lei/kWh; OPCOM publică lei/MWh."""
    return price_per_mwh / 1000.0


def monthly_settlement(exported_kwh: float, weighted_price_per_mwh: float) -> float:
    """Câștigul lunar al prosumatorului, în lei.

    Integrarea nu poate calcula ea însăși valoarea: nu citește un contor de
    energie exportată, iar registrele de energie ale invertorului numără
    încărcarea și descărcarea bateriei, nu injecția în rețea. Formula stă aici
    ca referință pentru șablonul din README, care alimentează câmpul
    `monthly_profit` al cardului.
    """
    return exported_kwh * lei_per_kwh(weighted_price_per_mwh)


def cheapest_window(
    series: PriceSeries, mtu_count: int, start_from: int = 0
) -> tuple[int, float]:
    """Fereastra contiguă de `mtu_count` intervale cu media cea mai mică.

    Pentru încărcare din rețea: `mtu_count = ceil(kWh_necesari / putere_kW * 4)`.
    Întoarce (index_start, preț_mediu).
    """
    values = series.values
    if mtu_count <= 0 or start_from + mtu_count > len(values):
        raise ValueError("mtu_count în afara seriei")

    window = sum(values[start_from : start_from + mtu_count])
    best_sum, best_start = window, start_from
    for i in range(start_from + mtu_count, len(values)):
        window += values[i] - values[i - mtu_count]
        if window < best_sum:
            best_sum, best_start = window, i - mtu_count + 1
    return best_start, best_sum / mtu_count


def most_expensive_window(
    series: PriceSeries, mtu_count: int, start_from: int = 0
) -> tuple[int, float]:
    """Simetricul lui `cheapest_window`, pentru fereastra de descărcare."""
    values = series.values
    if mtu_count <= 0 or start_from + mtu_count > len(values):
        raise ValueError("mtu_count în afara seriei")

    window = sum(values[start_from : start_from + mtu_count])
    best_sum, best_start = window, start_from
    for i in range(start_from + mtu_count, len(values)):
        window += values[i] - values[i - mtu_count]
        if window > best_sum:
            best_sum, best_start = window, i - mtu_count + 1
    return best_start, best_sum / mtu_count


def spread(series: PriceSeries) -> float:
    """Diferența vârf-gol a zilei. Sub costul de ciclare al bateriei, nu arbitrezi."""
    return series.maximum - series.minimum
