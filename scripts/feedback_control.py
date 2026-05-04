import time
import json
from datetime import datetime, timezone
from influxdb_client_3 import InfluxDBClient3
import paho.mqtt.client as mqtt

# ────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────
INFLUX_URL = "http://localhost:8181"
INFLUX_TOKEN = "apiv3_sIO1ekgndQdnqY5epVf2JERj1AG2E_HfPw_a3l0erRcKM7S8FkiJfT7UwzY1EQMij6-VSNchBsHuhYhx-d-5ig"
DB_NAME = "house_monitor"
MEASUREMENT = "smart_home"

# MQTT
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "home/control/command"

# Thresholds
SPIKE_THRESHOLD_WATTS = 6000

client_influx = InfluxDBClient3(host=INFLUX_URL, token=INFLUX_TOKEN)
client_mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

# ────────────────────────────────────────────────
# MQTT
# ────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    print(f"Connected to MQTT Broker with code {reason_code}")

client_mqtt.on_connect = on_connect

def start_mqtt():
    try:
        client_mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
        client_mqtt.loop_start()
    except Exception as e:
        print(f"MQTT Error: {e}")

def send_shutdown_command(room_key):
    """
    Sends the shutdown command.
    room_key corresponds to the names used in your Node-RED code: 'living', 'kitchen', etc.
    """
    payload = {
        "room": room_key,
        "action": "power_off",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    print(f" ANOMALY IDENTIFIED: Source = {room_key}")
    print(f"   -> Sending MQTT command: {payload}")
    
    try:
        info = client_mqtt.publish(MQTT_TOPIC, json.dumps(payload), qos=1)
        info.wait_for_publish()
    except Exception as e:
        print(f"Error sending: {e}")

# ────────────────────────────────────────────────
# ANALYSIS
# ────────────────────────────────────────────────
def analyze_system():
    # Retrieve the latest data
    query = f"""
    SELECT *
    FROM {MEASUREMENT}
    ORDER BY time DESC
    LIMIT 1
    """
    
    try:
        table = client_influx.query(query=query, database=DB_NAME)
        df = table.to_pandas()
        if df.empty: return
        
        data = df.iloc[0].to_dict()
        global_power = data.get('global_power_watts', 0)
        
        # 1. Sensor Error Detection
        if global_power < 0:
            print(" Anomaly: Sensor Error (Negative)")
            # A maintenance command could be sent here
            return

        # 2. Blackout Detection
        if global_power == 0:
            # Check if it's a real blackout (anomaly) or everything turned off
            # In your Node-RED code, the 'blackout' anomaly sets everything to 0.
            print(" Anomaly: Blackout detected")
            # Action: Try to re-engage?
            return

        # 3. High Spike Detection
        if global_power > SPIKE_THRESHOLD_WATTS:
            print(f" Anomaly: Global Spike ({global_power:.0f}W) > Threshold ({SPIKE_THRESHOLD_WATTS}W)")
            
            # --- SOURCE IDENTIFICATION ---
            # Map InfluxDB fields with Node-RED keys
            rooms_map = {
                "living_total_power": "living",
                "kitchen_total_power": "kitchen",
                "bed1_total_power": "bed1",
                "bed2_total_power": "bed2",
                "bath_total_power": "bath",
                "guest_total_power": "guest"
            }
            
            max_power = 0
            culprit_room_key = None
            
            for field_name, room_key in rooms_map.items():
                power = data.get(field_name, 0)
                if isinstance(power, (int, float)) and power > max_power:
                    max_power = power
                    culprit_room_key = room_key
            
            if culprit_room_key:
                send_shutdown_command(culprit_room_key)
            else:
                print("Source not identified")

    except Exception as e:
        print(f"Analysis Error: {e}")

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Starting Feedback Control Module (MQTT) ===")
    start_mqtt()
    
    while True:
        analyze_system()
        time.sleep(5) # Check every 5 seconds
