import asyncio
import sys
import re
import time
from playwright.async_api import async_playwright, Error as PlaywrightError, TargetClosedError

# =====================================================================
# CONFIGURATIE
# =====================================================================
RIGCTLD_HOST = "127.0.0.1"
RIGCTLD_PORT = 4532
WEBSDR_URL = "http://websdr.ewi.utwente.nl:8901/"

class RobustTwenteCATSync:
    def __init__(self):
        self.current_freq = 0
        self.is_tx = False
        self.rig_reader = None
        self.rig_writer = None
        self.running = True
        
        # Issue 5 Fix: Timestamp om feedback-loops te voorkomen
        self.last_hardware_update_time = 0.0
        
        # Issue 2 Fix: Regex om alleen getallen en decimalen te filteren
        self.numeric_regex = re.compile(r"[-+]?\d*\.\d+|\d+")

    async def close_rig_connection(self):
        """Issue 3 & 8 Fix: Sluit de socket-verbinding op een veilige manier af."""
        if self.rig_writer:
            try:
                self.rig_writer.close()
                await self.rig_writer.wait_closed()
            except Exception:
                pass  # Negeer fouten tijdens het geforceerd sluiten
        self.rig_writer = None
        self.rig_reader = None

    async def connect_rig(self) -> bool:
        """Probeert te verbinden met rigctld met een timeout."""
        await self.close_rig_connection()  # Zorg voor een schone start
        try:
            self.rig_reader, self.rig_writer = await asyncio.wait_for(
                asyncio.open_connection(RIGCTLD_HOST, RIGCTLD_PORT), timeout=2.0
            )
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError):
            return False
        except Exception as e:
            print(f"[Fout] Fout tijdens verbinden met rigctld: {e}")
            return False

    async def send_rig_cmd(self, cmd: str) -> str:
        """Stuurt een commando naar rigctld met foutafhandeling."""
        if not self.rig_writer:
            return ""
        try:
            self.rig_writer.write(f"{cmd}\n".encode())
            await self.rig_writer.drain()
            response = await asyncio.wait_for(self.rig_reader.readline(), timeout=1.0)
            return response.decode().strip()
        except (asyncio.TimeoutError, ConnectionResetError, AttributeError):
            print("\n[Waarschuwing] Verbinding met rigctld verloren. Herpoging volgt...")
            await self.close_rig_connection()  # Issue 3 Fix: Ruim dode socket op
            return ""

    async def test_hamlib_connection(self) -> bool:
        """Diagnostische testfase voor rigctld en de transceiver."""
        print("=" * 60)
        print(f"DIAGNOSTISCHE TEST: {RIGCTLD_HOST}:{RIGCTLD_PORT}...")
        print("=" * 60)
        
        if not await self.connect_rig():
            print("[FAIL] Geen verbinding met rigctld daemon. Draait het programma?")
            return False
        print("[OK]   Verbonden met rigctld daemon.")
        
        version_info = await self.send_rig_cmd("v")
        if version_info:
            print(f"[OK]   Hamlib reageert: {version_info}")
        else:
            print("[FAIL] Rigctld geeft geen geldige response.")
            return False
            
        freq_info = await self.send_rig_cmd("f")
        # Issue 7 Fix: Strip eventuele whitespace/statuskarakters van de rig-response
        if freq_info:
            freq_info = freq_info.strip()
            
        if freq_info and freq_info.isdigit():
            print(f"[OK]   Transceiver online! Actuele VFO: {int(freq_info)/1000} kHz")
            print("=" * 60)
            print("Test geslaagd! Browser start op...\n")
            return True
        else:
            print("[FAIL] Rigctld reageert, maar de transceiver is offline of onbereikbaar.")
            print("=" * 60)
            return False

    async def sync_loop(self, page):
        """Hoofd-synchronisatieloop met volledige foutafhandeling en loop-beveiliging."""
        print("[Sync] Synchronisatie actief.")
        
        while self.running:
            try:
                # Automatisch herstel van de rig-verbinding
                if not self.rig_writer:
                    if not await self.connect_rig():
                        await asyncio.sleep(2)
                        continue
                    else:
                        print("[Rig] Verbinding met rigctld succesvol hersteld!")

                current_time = time.time()

                # -------------------------------------------------------------
                # 1. PTT CONTROLEREN & AUDIO MUTEN
                # -------------------------------------------------------------
                ptt_str = await self.send_rig_cmd("t")
                if ptt_str:
                    ptt_str = ptt_str.strip()

                if ptt_str in ["0", "1"]:
                    is_transmitting = (ptt_str == "1")
                    if is_transmitting != self.is_tx:
                        self.is_tx = is_transmitting
                        print(f"[PTT] Status: {'ZENDEN' if self.is_tx else 'ONTVANGEN'}")
                        try:
                            # Issue 1 Fix: Gebruik de correcte, directe Page API voor audio muting
                            await page.set_audio_muted(self.is_tx)
                        except (PlaywrightError, TargetClosedError):
                            break  # Issue 4 Fix: Browser afgesloten

                # -------------------------------------------------------------
                # 2. FREQUENTIE SYNCHRONISATIE (Alleen tijdens RX)
                # -------------------------------------------------------------
                if not self.is_tx:
                    
                    # A. Van Hardware Radio -> WebSDR
                    freq_str = await self.send_rig_cmd("f")
                    if freq_str:
                        freq_str = freq_str.strip() # Issue 7 Fix

                    if freq_str and freq_str.isdigit():
                        rig_freq = int(freq_str)
                        if abs(rig_freq - self.current_freq) > 10:
                            self.current_freq = rig_freq
                            self.last_hardware_update_time = current_time # Issue 5 Fix: Zet timestamp
                            freq_khz = rig_freq / 1000.0
                            try:
                                await page.evaluate(f"window.setfreq({freq_khz})")
                            except (PlaywrightError, TargetClosedError):
                                break

                    # B. Van WebSDR -> Hardware Radio
                    # Issue 5 Fix: Negeer WebSDR wijzigingen binnen 400ms na een hardware-aanpassing
                    if current_time - self.last_hardware_update_time > 0.4:
                        try:
                            web_freq_str = await page.evaluate(
                                "() => document.getElementById('freqinput') ? document.getElementById('freqinput').value : null"
                            )
                            if web_freq_str:
                                # Issue 2 Fix: Extraheer robuust alleen de numerieke waarde (bijv. "3760.5 kHz" -> "3760.5")
                                match = self.numeric_regex.search(web_freq_str)
                                if match:
                                    web_freq_hz = int(float(match.group()) * 1000)
                                    if abs(web_freq_hz - self.current_freq) > 10:
                                        self.current_freq = web_freq_hz
                                        print(f"[WebSDR -> Rig] Frequentie aangepast op pagina: {match.group()} kHz")
                                        await self.send_rig_cmd(f"F {web_freq_hz}")
                        except (PlaywrightError, TargetClosedError):
                            break

            except Exception as e:
                print(f"[Loop Error] Onverwachte fout: {e}")
            
            await asyncio.sleep(0.2)

    async def main(self):
        if not await self.test_hamlib_connection():
            print("Applicatie afgebroken.")
            return

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=False)
                page = await browser.new_page()
                
                # Sluit netjes af als de browser handmatig wordt weggeklikt
                browser.on("disconnected", lambda: setattr(self, 'running', False))
                
                print(f"[Browser] Laden van {WEBSDR_URL}...")
                await page.goto(WEBSDR_URL)
                
                # Issue 6 Fix: Wacht totdat de WebSDR JavaScript API volledig geladen is
                print("[Browser] Wachten op initialisatie van WebSDR scripts...")
                await page.wait_for_function("() => typeof window.setfreq === 'function'", timeout=10000)
                
                # Start de hoofdloop
                await self.sync_loop(page)
                
            except (PlaywrightError, TargetClosedError): # Issue 4 Fix
                print(f"[Browser] Browser handmatig gesloten.")
            finally:
                print("[Info] Afsluiten... Sockets worden opgeruimd.")
                self.running = False
                await self.close_rig_connection() # Issue 8 Fix: Altijd de rig poort sluiten bij exit

if __name__ == "__main__":
    sync_app = RobustTwenteCATSync()
    try:
        asyncio.run(sync_app.main())
    except KeyboardInterrupt:
        print("\n[Info] CATSync gestopt via Ctrl+C.")