import time
from datetime import datetime, timezone
import pandas as pd
from prophet import Prophet
from influxdb_client_3 import InfluxDBClient3
import numpy as np
from sklearn.metrics import mean_squared_error

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
INFLUX_URL      = "http://localhost:8181"
INFLUX_TOKEN    = "apiv3_sIO1ekgndQdnqY5epVf2JERj1AG2E_HfPw_a3l0erRcKM7S8FkiJfT7UwzY1EQMij6-VSNchBsHuhYhx-d-5ig"
DB_NAME         = "house_monitor"
MEASUREMENT     = "smart_home"

HISTORY_HOURS   = 168   # 7 days
FORECAST_HOURS  = 6     # forecast next 6 hours

client = InfluxDBClient3(host=INFLUX_URL, token=INFLUX_TOKEN)

# ────────────────────────────────────────────────
# DATA LOADING
# ────────────────────────────────────────────────
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
        print("No data found...")
        return None
    
    df = df.rename(columns={"time": "ds", "global_power_watts": "y"})
    df["ds"] = pd.to_datetime(df["ds"])
    df = df[["ds", "y"]].dropna()
    
    print(f"Data loaded: {len(df)} points over {HISTORY_HOURS} hours")
    return df

# ────────────────────────────────────────────────
# PROPHET FORECAST
# ────────────────────────────────────────────────
def train_and_forecast_prophet(df):
    if df is None or len(df) < 24:
        print("Not enough data for Prophet")
        return None
    
    model = Prophet(
        yearly_seasonality=False,
        weekly_seasonality=True,
        daily_seasonality=True,
        seasonality_mode='multiplicative'
    )
    model.add_seasonality(name='hourly', period=1/24, fourier_order=5)
    model.fit(df)
    
    future = model.make_future_dataframe(periods=FORECAST_HOURS*12, freq='5min')
    forecast = model.predict(future)
    return forecast

# ────────────────────────────────────────────────
# WRITE TO INFLUX
# ────────────────────────────────────────────────
def write_forecast_to_influx(forecast, model_name="prophet"):
    forecast['ds'] = pd.to_datetime(forecast['ds'], utc=True)
    now_utc = datetime.now(timezone.utc)
    future_forecast = forecast[forecast['ds'] > now_utc]

    if future_forecast.empty:
        print(f"No future prediction to write for {model_name}")
        return

    if 'yhat_lower' in forecast.columns:
        df_out = future_forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].dropna()
    else:
        df_out = future_forecast[['ds', 'yhat']].dropna()

    lines = []
    for _, row in df_out.iterrows():
        ts = int(row['ds'].timestamp() * 1e9)  # nanoseconds
        if 'yhat_lower' in row:
            line = (
                f"power_forecast,model={model_name} "
                f"yhat={row['yhat']},"
                f"yhat_lower={row['yhat_lower']},"
                f"yhat_upper={row['yhat_upper']} "
                f"{ts}"
            )
        else:
            line = (
                f"power_forecast,model={model_name} "
                f"yhat={row['yhat']} "
                f"{ts}"
            )
        lines.append(line)

    try:
        print(f"Attempting to write {len(lines)} points ({model_name})...")
        client.write(
            database=DB_NAME,
            record="\n".join(lines)
        )
        print(f"Forecast {model_name} successfully written: {len(lines)} points")
    except Exception as e:
        print(f"WRITE ERROR {model_name}: {e}")

# ────────────────────────────────────────────────
# MAIN LOOP
# ────────────────────────────────────────────────
while True:
    print(f"\n--- New forecast at {datetime.now():%Y-%m-%d %H:%M:%S} ---")
    
    df = load_data()
    if df is not None:
        # Prophet
        forecast_prophet = train_and_forecast_prophet(df)
        if forecast_prophet is not None:
            write_forecast_to_influx(forecast_prophet, model_name="prophet")
            
    time.sleep(15 * 60)  # every 15 minutes
