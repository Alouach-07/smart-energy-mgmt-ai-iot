import time
from datetime import datetime, timezone
import pandas as pd
from prophet import Prophet
from influxdb_client_3 import InfluxDBClient3
import numpy as np

# ────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────
INFLUX_URL   = "http://localhost:8181"
INFLUX_TOKEN = "apiv3_sIO1ekgndQdnqY5epVf2JERj1AG2E_HfPw_a3l0erRcKM7S8FkiJfT7UwzY1EQMij6-VSNchBsHuhYhx-d-5ig"
DB_NAME      = "house_monitor"
MEASUREMENT  = "smart_home"

HISTORY_HOURS  = 168   # 7 days
FORECAST_HOURS = 6     # 6 hours forecast

client = InfluxDBClient3(host=INFLUX_URL, token=INFLUX_TOKEN)

def get_season_factor():
    """Determines the multiplier coefficient based on the current season"""
    month = datetime.now().month
    # WINTER: December, January, February
    if month in [12, 1, 2]:
        return 1.5
    # SPRING / AUTUMN (Mid-season): March, April, May, September, October, November
    elif month in [3, 4, 5, 9, 10, 11]:
        return 1.2
    # SUMMER: June, July, August
    else:
        return 1.0

def load_data():
    query = f"""
    SELECT time, global_power_watts
    FROM {MEASUREMENT}
    WHERE time >= now() - interval '{HISTORY_HOURS} hours'
    ORDER BY time ASC
    """
    table = client.query(query=query, database=DB_NAME)
    df = table.to_pandas()
    
    if df.empty:
        return None
    
    df = df.rename(columns={"time": "ds", "global_power_watts": "y"})
    # Remove timezone for Prophet compatibility
    df["ds"] = pd.to_datetime(df["ds"]).dt.tz_localize(None)
    return df.dropna()

def process_forecast(df):
    # 1. Historical spike analysis (Anomalies)
    # A spike is defined as > Mean + 3 standard deviations
    mean_y = df['y'].mean()
    std_y = df['y'].std()
    historical_spikes = df[df['y'] > mean_y + 3 * std_y]['y'] - mean_y
    
    # Default value if no spike is found in history (e.g., 8000W)
    base_spike_value = historical_spikes.mean() if not historical_spikes.empty else 8000
    
    # 2. Model training
    model = Prophet(
        yearly_seasonality=False,
        weekly_seasonality=True,
        daily_seasonality=True,
        seasonality_mode='multiplicative'
    )
    model.fit(df)
    
    # 3. Future creation
    future = model.make_future_dataframe(periods=FORECAST_HOURS*12, freq='5min')
    forecast = model.predict(future)
    
    # 4. Apply automatic seasonal factor
    factor = get_season_factor()
    forecast['dynamic_spike'] = base_spike_value * factor
    
    print(f"Season detected: Factor x{factor} applied. Estimated spike: {forecast['dynamic_spike'].iloc[-1]:.2f} W")
    
    return forecast

def write_to_influx(forecast):
    forecast['ds'] = pd.to_datetime(forecast['ds'], utc=True)
    now_utc = datetime.now(timezone.utc)
    # Keep only future predictions
    future_forecast = forecast[forecast['ds'] > now_utc]

    lines = []
    for _, row in future_forecast.iterrows():
        ts = int(row['ds'].timestamp() * 1e9)
        # Write forecasts AND the dynamic spike calculated for the season
        line = (
            f"power_forecast "
            f"yhat={row['yhat']:.2f},"
            f"yhat_upper={row['yhat_upper']:.2f},"
            f"dynamic_spike={row['dynamic_spike']:.2f} "
            f"{ts}"
        )
        lines.append(line)

    if lines:
        try:
            client.write(database=DB_NAME, record="\n".join(lines))
            print(f"✅ Recorded in InfluxDB: {len(lines)} forecast points.")
        except Exception as e:
            print(f"❌ Write error: {e}")

# --- MAIN LOOP ---
while True:
    print(f"\n--- Start calculation ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ---")
    data_df = load_data()
    
    if data_df is not None:
        result_forecast = process_forecast(data_df)
        write_to_influx(result_forecast)
    else:
        print("Waiting for data...")
    
    # Pause 15 minutes before next update
    time.sleep(15 * 60)
