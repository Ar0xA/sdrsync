import asyncio
import re
import time
import logging
from typing import Optional
from playwright.async_api import async_playwright, Error as PlaywrightError, TargetClosedError, Page

# =====================================================================
# CONFIGURATIE
# =====================================================================
RIGCTLD_HOST = "127.0.0.1"
RIGCTLD_PORT = 4532
WEBSDR_URL = "http://websdr.ewi.utwente.nl:8901/"

# Issue 15 Fix: voorheen verspreide magic numbers, nu centraal en benoemd.
RIGCTLD_CONNECT_TIMEOUT_S = 2.0     # Timeout voor het opzetten van de TCP-verbinding met rigctld
RIGCTLD_CMD_TIMEOUT_S = 1.0         # Timeout voor het wachten op een antwoord op een rigctld-commando
SYNC_LOOP_INTERVAL_S = 0.2          # Pauze tussen iteraties van de hoofd-synchronisatieloop
FREQ_DRIFT_THRESHOLD_HZ = 10        # Minimale afwijking (Hz) voordat een frequentiewijziging als "echt" geldt
SYNC_COOLDOWN_S = 0.4               # Afkoelperiode om echo/feedback-loops tussen Rig en WebSDR te voorkomen
FREQ_VERIFY_DELAY_S = 0.6           # Wachttijd (> SYNC_COOLDOWN_S) voordat we checken of WebSDR de freq. echt overnam
WEBSDR_LOAD_TIMEOUT_MS = 10000      # Timeout voor het wachten op initialisatie van de WebSDR-pagina
RIG_RECONNECT_BASE_DELAY_S = 2.0    # Basiswachttijd voor de eerste herverbindingspoging met rigctld
RIG_RECONNECT_MAX_DELAY_S = 30.0    # Bovengrens voor exponentiële backoff bij herverbinden
RIG_RECONNECT_WARN_AFTER = 5        # Aantal opeenvolgende mislukte pogingen voordat een WARNING i.p.v. DEBUG geeft

# Issue 16 Fix: gestructureerde logging i.p.v. kale print()-statements, met tijdstempels,
# niveaus (DEBUG/INFO/WARNING/ERROR) en zowel console- als bestandsoutput. Dit maakt het
# mogelijk om achteraf (of live via `tail -f`) precies te zien wanneer en waarom iets misging,
# ook voor gebeurtenissen die te snel voorbijkomen om in een interactieve terminal te lezen.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                       # zichtbaar in de terminal
        logging.FileHandler("websdr_sync.log", mode="a", encoding="utf-8"),  # persistent logbestand
    ],
)
logger = logging.getLogger("websdrSync")

# Issue 14 Fix: JavaScript die eenmalig bij het laden van de pagina wordt geïnjecteerd.
# In plaats van bij elke PTT-wijziging een losse querySelectorAll-snapshot te nemen (die
# audio/video-elementen mist die pas ná die snapshot worden aangemaakt), zet dit een
# MutationObserver op die de mute-status blijvend afdwingt op alle huidige én toekomstige
# <audio>/<video>-elementen. Python hoeft alleen nog window.__catSyncSetMuted(muted) aan
# te roepen; de observer houdt de rest bij.
MUTE_OBSERVER_INIT_JS = """
() => {
    window.__catSyncMuted = false;
    window.__catSyncApplyMute = () => {
        document.querySelectorAll('audio, video').forEach((el) => { el.muted = window.__catSyncMuted; });
    };
    if (window.__catSyncObserver) {
        window.__catSyncObserver.disconnect();
    }
    window.__catSyncObserver = new MutationObserver(() => window.__catSyncApplyMute());
    window.__catSyncObserver.observe(document.documentElement, { childList: true, subtree: true });
    window.__catSyncSetMuted = (muted) => {
        window.__catSyncMuted = muted;
        window.__catSyncApplyMute();
        return document.querySelectorAll('audio, video').length;
    };
}
"""

# =====================================================================
# CHANGELOG (fixes t.o.v. de vorige versie)
# =====================================================================
# Issue 9  : PTT-status 2 (TX mic) en 3 (TX data) worden nu ook als "zenden" herkend,
#            niet alleen status 1 (per rigctl/rigctld protocolspecificatie).
# Issue 10 : page.set_audio_muted() bestaat niet in Playwright's Python API (geverifieerd
#            tegen de officiële documentatie en issue tracker) en gaf een ongevangen
#            AttributeError. Vervangen door DOM-gebaseerde muting.
# Issue 11 : Symmetrische cooldown (last_websdr_update_time) toegevoegd zodat een
#            WebSDR->Rig wijziging niet direct wordt overschreven door een Rig->WebSDR
#            leesactie die de rig nog niet had verwerkt.
# Issue 12 : De browser werd nooit expliciet gesloten bij een niet-fatale afsluiting
#            (bijv. een navigatie-timeout), wat een achtergebleven browserproces kon
#            veroorzaken. Nu altijd sluiten in de finally-blok, ook bij non-fatal paths.
# Issue 13 : Het antwoord van rigctld op het "F" (set-frequency) commando werd nooit
#            gecontroleerd. RPRT-foutcodes worden nu gelogd i.p.v. stilzwijgend genegeerd.
# Issue 14 : Zie MUTE_OBSERVER_INIT_JS hierboven - muting werkt nu ook voor audio/video-
#            elementen die na de laatste PTT-wisseling zijn aangemaakt, en het aantal
#            gemute elementen wordt teruggerapporteerd (0 = mogelijk signaal dat de site
#            geen HTML media-elementen gebruikt voor audio).
# Issue 15 : Magic numbers gecentraliseerd als benoemde constanten (zie CONFIGURATIE).
# Issue 16 : print() vervangen door het standaard logging-mechanisme.
# Issue 17 : Herverbindingslogica met rigctld gebruikt nu exponentiële backoff (max
#            RIG_RECONNECT_MAX_DELAY_S) i.p.v. een vaste 2s-poging, met een oplopende
#            waarschuwing na RIG_RECONNECT_WARN_AFTER opeenvolgende mislukkingen.
# Issue 18 : Ongebruikte `import sys` verwijderd.
# Issue 19 : Type hints toegevoegd voor `page: Page`-parameters.
# Issue 20 : Browser-side diagnostiek (nieuw, op verzoek):
#              a) page.on("console") en page.on("pageerror") vangen console-output en
#                 onafgehandelde JS-fouten van de WebSDR-pagina zelf op, zodat problemen
#                 aan de site-kant zichtbaar worden i.p.v. verborgen te blijven.
#              b) Na elke window.setfreq()-aanroep controleert een losstaande achtergrond-
#                 taak ~0.6s later of #freqinput daadwerkelijk de gevraagde waarde toont.
#                 Zo niet, dan wordt dit gelogd als waarschuwing. Dit verandert het
#                 syncgedrag NIET - het is puur diagnostisch, bedoeld om te helpen bepalen
#                 of/waarom de WebSDR-pagina niet reageert zoals verwacht.
# =====================================================================


class RobustTwenteCATSync:
    def __init__(self):
        self.current_freq = 0
        self.is_tx = False
        self.rig_reader = None
        self.rig_writer = None
        self.running = True

        # Issue 5 Fix: cooldown Rig -> WebSDR richting
        self.last_hardware_update_time = 0.0
        # Issue 11 Fix: cooldown WebSDR -> Rig richting
        self.last_websdr_update_time = 0.0

        # Issue 2 Fix: regex om alleen getallen en decimalen te filteren
        self.numeric_regex = re.compile(r"[-+]?\d*\.\d+|\d+")

        # Issue 17 Fix: state voor exponentiële reconnect-backoff
        self._reconnect_failures = 0

        # Issue 14 Fix: voorkomt dat de "0 elementen gevonden"-waarschuwing bij elke
        # PTT-wisseling opnieuw gelogd wordt (zou anders spammen).
        self._mute_zero_warning_shown = False

        # Issue 20 Fix: referentie naar de lopende freq-verificatietaak, zodat een
        # oudere verificatie geannuleerd kan worden als er alweer een nieuwe
        # frequentiewijziging binnenkomt voordat de vorige check klaar is.
        self._freq_verify_task: Optional[asyncio.Task] = None

    # -------------------------------------------------------------------
    # Rig-verbinding
    # -------------------------------------------------------------------
    async def close_rig_connection(self):
        """Issue 3 & 8 Fix: sluit de socket-verbinding op een veilige manier af."""
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
                asyncio.open_connection(RIGCTLD_HOST, RIGCTLD_PORT), timeout=RIGCTLD_CONNECT_TIMEOUT_S
            )
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError):
            return False
        except Exception as e:
            logger.error(f"[Fout] Fout tijdens verbinden met rigctld: {e}")
            return False

    async def send_rig_cmd(self, cmd: str) -> str:
        """Stuurt een commando naar rigctld met foutafhandeling."""
        if not self.rig_writer:
            return ""
        try:
            self.rig_writer.write(f"{cmd}\n".encode())
            await self.rig_writer.drain()
            response = await asyncio.wait_for(self.rig_reader.readline(), timeout=RIGCTLD_CMD_TIMEOUT_S)
            return response.decode().strip()
        except (asyncio.TimeoutError, ConnectionResetError, AttributeError):
            logger.warning("[Waarschuwing] Verbinding met rigctld verloren. Herpoging volgt...")
            await self.close_rig_connection()  # Issue 3 Fix: ruim dode socket op
            return ""

    async def _interruptible_sleep(self, seconds: float):
        """
        Issue 17 Fix: slaapt in kleine stappen zodat self.running tussentijds
        gecontroleerd wordt. Voorkomt dat een afsluitverzoek tijdens een lange
        backoff-wachttijd onnodig lang op zich laat wachten.
        """
        remaining = seconds
        step = 0.5
        while remaining > 0 and self.running:
            await asyncio.sleep(min(step, remaining))
            remaining -= step

    # -------------------------------------------------------------------
    # Browser-side audio muting (Issue 10 & 14 Fix)
    # -------------------------------------------------------------------
    async def set_page_audio_muted(self, page: Page, muted: bool) -> None:
        """
        Zet de mute-status via de MutationObserver-hook die bij het laden van de
        pagina is geïnjecteerd (zie MUTE_OBSERVER_INIT_JS). Rapporteert hoeveel
        <audio>/<video>-elementen daadwerkelijk geraakt zijn, zodat je kunt zien
        of de aanpak überhaupt iets doet op deze specifieke WebSDR-installatie.

        Fouten zijn expliciet onderverdeeld in fataal (browser echt weg, propageert
        verder) versus niet-fataal (bijv. tijdelijke JS-fout), zodat een enkele
        mislukte mute-poging niet de hele sync-loop laat crashen.
        """
        try:
            element_count = await page.evaluate("(muted) => window.__catSyncSetMuted(muted)", muted)
            if element_count == 0:
                if not self._mute_zero_warning_shown:
                    logger.warning(
                        "[Audio] Geen <audio>/<video>-elementen gevonden om te muten. "
                        "Mogelijk gebruikt deze WebSDR de Web Audio API (AudioContext) i.p.v. "
                        "HTML media-elementen - in dat geval heeft DOM-gebaseerde muting geen effect "
                        "en moet de pagina's audio-pijplijn handmatig geïnspecteerd worden."
                    )
                    self._mute_zero_warning_shown = True
            else:
                logger.debug(f"[Audio] {element_count} media-element(en) {'gemute' if muted else 'ge-unmute'}.")
        except TargetClosedError:
            raise  # Browser/tab echt gesloten - fataal, laat de aanroeper dit afhandelen.
        except PlaywrightError as e:
            logger.warning(f"[Waarschuwing] Audio mute-poging mislukt (niet-fataal): {e}")

    # -------------------------------------------------------------------
    # Browser-side diagnostiek (Issue 20 Fix)
    # -------------------------------------------------------------------
    def _on_page_console(self, msg) -> None:
        """Vangt console.log/warn/error van de WebSDR-pagina zelf op."""
        if msg.type in ("error", "warning"):
            logger.warning(f"[Browser console:{msg.type}] {msg.text}")
        else:
            logger.debug(f"[Browser console:{msg.type}] {msg.text}")

    def _on_page_error(self, exc) -> None:
        """Vangt onafgehandelde JS-exceptions op de WebSDR-pagina zelf op (niet van onze eigen evaluate-calls)."""
        logger.error(f"[Browser JS-fout] Onafgehandelde JS-exceptie op WebSDR-pagina: {exc}")

    async def verify_freq_applied(self, page: Page, expected_hz: int) -> None:
        """
        Issue 20 Fix: controleert, los van de hoofdloop, of de WebSDR-pagina de
        laatst aangevraagde frequentie daadwerkelijk heeft overgenomen. Wacht
        FREQ_VERIFY_DELAY_S (> SYNC_COOLDOWN_S) zodat de cooldown-periode in de
        hoofdloop niet interfereert met deze controle. Verandert GEEN state en
        stuurt GEEN commando's - dit is puur observeren en loggen, om te helpen
        bepalen of window.setfreq() op deze pagina daadwerkelijk het gewenste
        effect heeft.
        """
        try:
            await asyncio.sleep(FREQ_VERIFY_DELAY_S)
            web_freq_str = await page.evaluate(
                "() => document.getElementById('freqinput') ? document.getElementById('freqinput').value : null"
            )
            if not web_freq_str:
                logger.warning("[Verificatie] Kon 'freqinput' niet uitlezen na setfreq() aanroep - element niet gevonden.")
                return

            match = self.numeric_regex.search(web_freq_str)
            if not match:
                logger.warning(f"[Verificatie] Geen numerieke waarde te herleiden uit freqinput: '{web_freq_str}'")
                return

            actual_hz = int(float(match.group()) * 1000)
            if abs(actual_hz - expected_hz) > FREQ_DRIFT_THRESHOLD_HZ:
                logger.warning(
                    f"[Verificatie] WebSDR lijkt de gevraagde frequentie NIET te hebben overgenomen: "
                    f"gevraagd={expected_hz / 1000:.3f} kHz, waargenomen={actual_hz / 1000:.3f} kHz. "
                    f"Mogelijk reageert window.setfreq() anders dan verwacht op deze pagina."
                )
            else:
                logger.debug(f"[Verificatie] WebSDR bevestigd op {actual_hz / 1000:.3f} kHz.")
        except TargetClosedError:
            pass  # Browser gesloten tijdens verificatie - niets te loggen/doen
        except asyncio.CancelledError:
            pass  # Vervangen door een nieuwere verificatietaak - geen probleem
        except PlaywrightError as e:
            logger.warning(f"[Verificatie] Fout tijdens verifiëren van setfreq()-resultaat: {e}")

    def _schedule_freq_verification(self, page: Page, expected_hz: int) -> None:
        """Annuleert een eventuele lopende verificatietaak en start een nieuwe."""
        if self._freq_verify_task is not None and not self._freq_verify_task.done():
            self._freq_verify_task.cancel()
        self._freq_verify_task = asyncio.create_task(self.verify_freq_applied(page, expected_hz))

    # -------------------------------------------------------------------
    # Diagnose
    # -------------------------------------------------------------------
    async def test_hamlib_connection(self) -> bool:
        """Diagnostische testfase voor rigctld en de transceiver."""
        logger.info("=" * 60)
        logger.info(f"DIAGNOSTISCHE TEST: {RIGCTLD_HOST}:{RIGCTLD_PORT}...")
        logger.info("=" * 60)

        if not await self.connect_rig():
            logger.error("[FAIL] Geen verbinding met rigctld daemon. Draait het programma?")
            return False
        logger.info("[OK]   Verbonden met rigctld daemon.")

        version_info = await self.send_rig_cmd("v")
        if version_info:
            logger.info(f"[OK]   Hamlib reageert: {version_info}")
        else:
            logger.error("[FAIL] Rigctld geeft geen geldige response.")
            return False

        freq_info = await self.send_rig_cmd("f")
        # Issue 7 Fix: strip eventuele whitespace/statuskarakters van de rig-response
        if freq_info:
            freq_info = freq_info.strip()

        if freq_info and freq_info.isdigit():
            logger.info(f"[OK]   Transceiver online! Actuele VFO: {int(freq_info) / 1000} kHz")
            logger.info("=" * 60)
            logger.info("Test geslaagd! Browser start op...")
            return True
        else:
            logger.error("[FAIL] Rigctld reageert, maar de transceiver is offline of onbereikbaar.")
            logger.info("=" * 60)
            return False

    # -------------------------------------------------------------------
    # Hoofdloop
    # -------------------------------------------------------------------
    async def sync_loop(self, page: Page):
        """Hoofd-synchronisatieloop met volledige foutafhandeling en loop-beveiliging."""
        logger.info("[Sync] Synchronisatie actief.")

        while self.running:
            try:
                # -------------------------------------------------------------
                # 0. AUTOMATISCH HERSTEL VAN DE RIG-VERBINDING (Issue 17 Fix)
                # -------------------------------------------------------------
                if not self.rig_writer:
                    if not await self.connect_rig():
                        self._reconnect_failures += 1
                        delay = min(
                            RIG_RECONNECT_BASE_DELAY_S * (2 ** (self._reconnect_failures - 1)),
                            RIG_RECONNECT_MAX_DELAY_S,
                        )
                        if self._reconnect_failures % RIG_RECONNECT_WARN_AFTER == 0:
                            logger.warning(
                                f"[Rig] {self._reconnect_failures} opeenvolgende mislukte pogingen om te "
                                f"verbinden met rigctld. Volgende poging over {delay:.1f}s."
                            )
                        else:
                            logger.debug(
                                f"[Rig] Verbindingspoging {self._reconnect_failures} mislukt. "
                                f"Volgende poging over {delay:.1f}s."
                            )
                        await self._interruptible_sleep(delay)
                        continue
                    else:
                        if self._reconnect_failures > 0:
                            logger.info(
                                f"[Rig] Verbinding met rigctld succesvol hersteld na "
                                f"{self._reconnect_failures} mislukte poging(en)!"
                            )
                        self._reconnect_failures = 0

                current_time = time.time()

                # -------------------------------------------------------------
                # 1. PTT CONTROLEREN & AUDIO MUTEN
                # -------------------------------------------------------------
                ptt_str = await self.send_rig_cmd("t")
                if ptt_str:
                    ptt_str = ptt_str.strip()

                # Issue 9 Fix: get_ptt kan 0 (RX), 1 (TX), 2 (TX mic) of 3 (TX data)
                # teruggeven, afhankelijk van de rig-backend.
                if ptt_str in ("0", "1", "2", "3"):
                    is_transmitting = ptt_str != "0"
                    if is_transmitting != self.is_tx:
                        self.is_tx = is_transmitting
                        logger.info(f"[PTT] Status: {'ZENDEN' if self.is_tx else 'ONTVANGEN'} (raw='{ptt_str}')")
                        try:
                            await self.set_page_audio_muted(page, self.is_tx)
                        except TargetClosedError:
                            break  # Issue 4 Fix: browser afgesloten
                elif ptt_str:
                    logger.warning(f"[Waarschuwing] Onbekende PTT-waarde ontvangen van rigctld: '{ptt_str}'")

                # -------------------------------------------------------------
                # 2. FREQUENTIE SYNCHRONISATIE (alleen tijdens RX)
                # -------------------------------------------------------------
                if not self.is_tx:

                    # A. Van Hardware Radio -> WebSDR
                    freq_str = await self.send_rig_cmd("f")
                    if freq_str:
                        freq_str = freq_str.strip()  # Issue 7 Fix

                    if freq_str and freq_str.isdigit():
                        rig_freq = int(freq_str)
                        if abs(rig_freq - self.current_freq) > FREQ_DRIFT_THRESHOLD_HZ:
                            # Issue 11 Fix: geef de rig de kans om een recente WebSDR->Rig
                            # wijziging te verwerken voordat we een mogelijk verouderde
                            # leeswaarde terugsturen naar de pagina.
                            if current_time - self.last_websdr_update_time > SYNC_COOLDOWN_S:
                                self.current_freq = rig_freq
                                self.last_hardware_update_time = current_time  # Issue 5 Fix
                                freq_khz = rig_freq / 1000.0
                                try:
                                    await page.evaluate(f"window.setfreq({freq_khz})")
                                    # Issue 20 Fix: controleer op de achtergrond of de pagina
                                    # dit ook echt heeft overgenomen.
                                    self._schedule_freq_verification(page, rig_freq)
                                except TargetClosedError:
                                    break
                                except PlaywrightError as e:
                                    logger.warning(f"[Waarschuwing] setfreq() aanroep mislukt (niet-fataal): {e}")

                    # B. Van WebSDR -> Hardware Radio
                    # Issue 5 Fix: negeer WebSDR wijzigingen binnen SYNC_COOLDOWN_S na een hardware-aanpassing
                    if current_time - self.last_hardware_update_time > SYNC_COOLDOWN_S:
                        try:
                            web_freq_str = await page.evaluate(
                                "() => document.getElementById('freqinput') ? document.getElementById('freqinput').value : null"
                            )
                            if web_freq_str:
                                # Issue 2 Fix: extraheer robuust alleen de numerieke waarde
                                match = self.numeric_regex.search(web_freq_str)
                                if match:
                                    web_freq_hz = int(float(match.group()) * 1000)
                                    if abs(web_freq_hz - self.current_freq) > FREQ_DRIFT_THRESHOLD_HZ:
                                        self.current_freq = web_freq_hz
                                        # Issue 11 Fix: markeer dit moment zodat de Rig->WebSDR
                                        # richting even pauzeert totdat de rig dit verwerkt heeft.
                                        self.last_websdr_update_time = current_time
                                        logger.info(f"[WebSDR -> Rig] Frequentie aangepast op pagina: {match.group()} kHz")

                                        # Issue 13 Fix: controleer het RPRT-antwoord van rigctld
                                        # op het set-frequency commando i.p.v. het te negeren.
                                        rprt = await self.send_rig_cmd(f"F {web_freq_hz}")
                                        if rprt:
                                            if rprt.startswith("RPRT"):
                                                parts = rprt.split()
                                                if len(parts) >= 2 and parts[1] != "0":
                                                    logger.warning(
                                                        f"[Rig] Frequentie instellen ({web_freq_hz} Hz) "
                                                        f"afgewezen door rigctld: '{rprt}'"
                                                    )
                                            else:
                                                logger.debug(f"[Rig] Onverwacht antwoord op 'F' commando: '{rprt}'")
                                        else:
                                            logger.warning(
                                                f"[Rig] Geen antwoord op frequentie-instelcommando voor "
                                                f"{web_freq_hz} Hz (mogelijk verbinding verloren)."
                                            )
                        except TargetClosedError:
                            break
                        except PlaywrightError as e:
                            logger.warning(f"[Waarschuwing] Uitlezen freqinput mislukt (niet-fataal): {e}")

            except Exception as e:
                # exc_info=True logt de volledige stacktrace - belangrijk voor debugging
                # van onverwachte fouten die niet door de specifiekere except-blokken
                # hierboven zijn afgevangen.
                logger.error(f"[Loop Error] Onverwachte fout: {e}", exc_info=True)

            await asyncio.sleep(SYNC_LOOP_INTERVAL_S)

    # -------------------------------------------------------------------
    # Entry point
    # -------------------------------------------------------------------
    async def main(self):
        if not await self.test_hamlib_connection():
            logger.error("Applicatie afgebroken.")
            return

        async with async_playwright() as p:
            browser = None
            try:
                browser = await p.chromium.launch(headless=False)
                page = await browser.new_page()

                # Issue 20 Fix: browser-side JS-diagnostiek - vang console-meldingen en
                # onafgehandelde JS-fouten van de WebSDR-pagina zelf op.
                page.on("console", self._on_page_console)
                page.on("pageerror", self._on_page_error)

                browser.on("disconnected", lambda: setattr(self, 'running', False))

                logger.info(f"[Browser] Laden van {WEBSDR_URL}...")
                await page.goto(WEBSDR_URL)

                # Issue 6 Fix: wacht totdat de WebSDR JavaScript API volledig geladen is
                logger.info("[Browser] Wachten op initialisatie van WebSDR scripts...")
                await page.wait_for_function("() => typeof window.setfreq === 'function'", timeout=WEBSDR_LOAD_TIMEOUT_MS)

                # Issue 14 Fix: injecteer de MutationObserver-hook voor persistente audio-muting
                await page.evaluate(MUTE_OBSERVER_INIT_JS)

                # Start de hoofdloop
                await self.sync_loop(page)

            except (PlaywrightError, TargetClosedError):  # Issue 4 Fix
                logger.info("[Browser] Browser handmatig gesloten of onbereikbaar.")
            finally:
                logger.info("[Info] Afsluiten... sockets en browser worden opgeruimd.")
                self.running = False

                if self._freq_verify_task is not None and not self._freq_verify_task.done():
                    self._freq_verify_task.cancel()

                await self.close_rig_connection()  # Issue 8 Fix: altijd de rig poort sluiten bij exit

                # Issue 12 Fix: browser altijd expliciet sluiten, ook bij niet-fatale
                # excepties, om achtergebleven browserprocessen te voorkomen.
                if browser is not None:
                    try:
                        if browser.is_connected():
                            await browser.close()
                    except Exception as e:
                        logger.warning(f"[Waarschuwing] Fout bij sluiten van browser (niet-fataal): {e}")


if __name__ == "__main__":
    sync_app = RobustTwenteCATSync()
    try:
        asyncio.run(sync_app.main())
    except KeyboardInterrupt:
        logger.info("[Info] CATSync gestopt via Ctrl+C.")
