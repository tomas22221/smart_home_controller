from tuya_connector import TuyaOpenAPI
import smtplib
from email.mime.text import MIMEText
import time
import json
import os
import requests
from datetime import datetime

ACCESS_ID = os.environ.get("ACCESS_ID", "")
ACCESS_SECRET = os.environ.get("ACCESS_SECRET", "")
ENDPOINT = "https://openapi.tuyaeu.com"
DEVICE_ID = os.environ.get("DEVICE_ID", "")

openapi = TuyaOpenAPI(ENDPOINT, ACCESS_ID, ACCESS_SECRET)
openapi.connect()

EMAIL = "lipinko231@gmail.com"
RECEIVER_EMAIL = "tom.lipensky@outlook.com"
PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
OWM_API_KEY = os.environ.get("OWM_API_KEY", "")  

MESTO = "Brno"

TARGET_TEMP = 22.0
T1 = 21.5   # Minimální komfortní teplota
T2 = 22.5   # Maximální komfortní teplota

THRESHOLD_NORMAL = 400   # Návrat do normálu
THRESHOLD_SLOW   = 500   # e-mail
THRESHOLD_HARD   = 600   # topení + e-mail

CHECK_INTERVAL = 60


class SmartHVAC:
    def __init__(self):
        self.last_state = "normal"
        self.fan_state = False

    def get_humidity(self) -> float | None:
        """Načte aktuální vlhkost z Tuya zařízení."""
        try:
            response = openapi.get(f"/v1.0/devices/{DEVICE_ID}/status")
            print(json.dumps(response, indent=4))
            if response.get("success"):
                for item in response.get("result", []):
                    if item["code"] == "va_humidity":
                        return float(item["value"])
            return None
        except Exception as e:
            print(f"⚠️ Chyba při čtení vlhkosti z Tuya: {e}")
            return None

    def get_weather_data(self) -> dict | None:
        """Získá data o počasí a předpověď z OpenWeatherMap."""
        try:
            url = (
                f"https://api.openweathermap.org/data/2.5/forecast"
                f"?q={MESTO}&appid={OWM_API_KEY}&units=metric&lang=cz"
            )
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            temps = [x["main"]["temp"] for x in data["list"][:8]]
            return {
                "Tv":          data["list"][0]["main"]["temp"],
                "Vv":          data["list"][0]["main"]["humidity"],
                "future_temp": data["list"][1]["main"]["temp"],
                "Tn":          min(temps),   # FIX: bylo prohozeno — Tn = minimum
                "Tx":          max(temps),   # FIX: Tx = maximum
            }
        except Exception as e:
            print(f"⚠️ Chyba OpenWeatherMap API: {e}")
            return None

    def send_email_notification(self, subject: str, message: str):
        """Univerzální funkce pro odesílání e-mailů."""
        try:
            msg = MIMEText(message, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"]    = EMAIL
            msg["To"]      = RECEIVER_EMAIL
            with smtplib.SMTP("smtp.gmail.com", 587) as server:
                server.starttls()
                server.login(EMAIL, PASSWORD)
                server.sendmail(EMAIL, RECEIVER_EMAIL, msg.as_string())
            print(f"📧 E-mail odeslán: {subject}")
        except Exception as e:
            print(f"❌ Selhalo odeslání e-mailu: {e}")

    def toggle_heating_device(self, turn_on: bool):
        """Ovládání Tuya zásuvky přes API."""
        endpoint = f"/v1.0/devices/{DEVICE_ID}/commands"
        payload  = {"commands": [{"code": "switch_1", "value": turn_on}]}
        try:
            response = openapi.post(endpoint, payload)
            if response.get("success"):
                print(f"✅ Tuya zařízení {'ZAPNUTO' if turn_on else 'VYPNUTO'}")
            else:
                print(f"❌ Chyba Tuya API: {response.get('msg')}")
        except Exception as e:
            print(f"❌ Selhalo spojení s Tuya API: {e}")

    def set_ventilation_fan(self, state: bool, reason: str):
        """Logování / ovládání ventilátoru."""
        if self.fan_state != state:
            self.fan_state = state
            print(f"💨 VĚTRÁNÍ {'ZAPNUTO' if state else 'VYPNUTO'} (Důvod: {reason})")

    def check_humidity_and_states(self, humidity: float, weather: dict | None):
        """Vyhodnocení stavu vlhkosti, teploty a předpovědi."""
        # Určení stavu vlhkosti
        if humidity >= THRESHOLD_HARD:
            current_state = "hard"
        elif humidity >= THRESHOLD_SLOW:
            current_state = "slow"
        elif humidity <= THRESHOLD_NORMAL:
            current_state = "normal"
        else:
            current_state = self.last_state  # hystereze — zůstaň v posledním stavu

        # Reakce na změnu stavu
        if current_state != self.last_state:
            human_readable = humidity / 10
            if current_state == "slow":
                self.send_email_notification(
                    "⚠️ Vysoká vlhkost",
                    f"SLOW ALERT – vlhkost stoupla na {human_readable:.1f}%"
                )
            elif current_state == "hard":
                self.send_email_notification(
                    "🚨 Příliš vysoká vlhkost",
                    f"HARD ALERT – vlhkost stoupla na {human_readable:.1f}%, zapnuto topení"
                )
                self.toggle_heating_device(True)
            elif current_state == "normal":
                print("✨ Vlhkost je zpět v normálu")
                self.toggle_heating_device(False)

        self.last_state = current_state

        # Logika větrání (pouze pokud jsou data z počasí)
        if weather:
            inside_temp = 23.1  # TODO: nahraď reálným čidlem
            outside_humidity_scaled = weather["Vv"] * 10

            new_fan_state = self.fan_state
            reason = "Udržování stavu větrání"

            if inside_temp < T1:
                new_fan_state = False
                reason = "V domě je příliš chladno"
            elif humidity > THRESHOLD_SLOW and outside_humidity_scaled < humidity:
                new_fan_state = True
                reason = "Vysoká vnitřní vlhkost (venku je sušeji)"
            elif inside_temp > T2 and weather["Tv"] < inside_temp:
                new_fan_state = True
                reason = "Chlazení domu venkovním vzduchem"
            elif weather["future_temp"] > (TARGET_TEMP + 3) and weather["Tv"] < inside_temp and inside_temp > T1:
                new_fan_state = True
                reason = "Předchlazení před horkým dnem"
            elif weather["future_temp"] < TARGET_TEMP and inside_temp <= TARGET_TEMP:
                new_fan_state = False
                reason = "Příprava na venkovní ochlazení"

            self.set_ventilation_fan(new_fan_state, reason)

    def run_cycle(self):
        """Jeden krok hlavní smyčky."""
        print(f"\n--- Kontrola: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
        humidity = self.get_humidity()
        print(f"Aktuální vlhkost z čidla: {humidity}")
        if humidity is not None:
            weather = self.get_weather_data()
            self.check_humidity_and_states(humidity, weather)
        else:
            print("❌ Selhal odpočet vlhkosti, přeskočeno vyhodnocení.")

if __name__ == "__main__":
    print("▶ START: Kompletní HVAC systém spuštěn")
    hvac = SmartHVAC()
    while True:
        try:
            hvac.run_cycle()
        except Exception as e:
            print(f"💥 Neočekávaná chyba v hlavním cyklu: {e}")
        time.sleep(CHECK_INTERVAL)