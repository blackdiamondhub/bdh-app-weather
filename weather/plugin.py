from bdh_sdk import PluginSpec, Setting, Tile
from bdh_sdk.plugins import LIFECYCLE_ON_DEMAND, NETWORK_OUTBOUND
from bdh_sdk.provides import STATS

STAT_KEYS = (
    ("temp", "Temperature"),
    ("feels_like", "Feels like"),
    ("sunrise", "Sunrise"),
    ("sunset", "Sunset"),
    ("sun", "Next sunrise or sunset"),
)

spec = PluginSpec(
    id="weather",
    app="weather",
    urls="weather.urls",
    status="weather.homepage.status",
    provides={STATS: "weather.provides.stats"},
    stat_keys=STAT_KEYS,
    # Read-only weather lookups for the hub's assistant, in every mode.
    tools="weather.assistant.tools",
    run_tool="weather.assistant.run_tool",
    tiles=(
        Tile(
            title="Weather",
            url_name="weather",
            icon="weather/hub/icon.svg",
            section="more",
            subtitle="Now, 24 hours & 7 days",
            order=10,
        ),
    ),
    settings=(
        Setting("place", "Place name", default="",
                help="Shown on the Now card. Defaults to the city in Settings > General."),
    ),
    api_version=2,
    # Only reads its own setting and the hub's location/units (open to every app).
    permissions=(),
    # Fetches Open-Meteo, Environment Canada and NWS.
    network=NETWORK_OUTBOUND,
    # Forecasts are fetched on request and cached; the hub's status/stats hooks
    # wake the app when it's stopped.
    lifecycle=LIFECYCLE_ON_DEMAND,
    memory_mb=96,
    # Refresh the tile's temperature a few times a day while asleep.
    wake_every=3 * 3600,
)
