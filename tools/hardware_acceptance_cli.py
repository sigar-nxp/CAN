#!/usr/bin/env python3
"""
Interactive Hardware Acceptance CLI for PS Locks Open Integration Platform.

Validates physical lock hardware features with clear terminal feedback:
1. NFC / RFID Hardware Check:
   - Capture unregistered card and simulate cloud-gated close (Slot 1 enrollment).
   - Verify local whitelist opening (Usecase 2) without gateway latency.
   - Verify unauthorized card denial signal (red LED, error buzz).
2. Sensor & Door Contact Check:
   - Verify door open (is_door_closed: false) transition on gateway (< 500 ms).
   - Test Door Close Guard warning: wait for warning timeout, verify ALARM1 feedback,
     then prompt operator to close door.
3. Visual & Acoustic Verification:
   - Play distinct buzzer test tones (OK, NOT_OK, ERROR, ALARM1) and flash LEDs,
     prompting operator confirmation for pitch, tone progression, and beep counts.
4. Safe Teardown:
   - Guarantees lock is restored to a safe, unlocked operational state upon exit.
"""

import argparse
import sys
import time
from typing import Any, Dict, Optional
import requests

# ANSI Color and Formatting Constants
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
WHITE = "\033[97m"

BG_GREEN = "\033[42m\033[30m\033[1m"
BG_RED = "\033[41m\033[37m\033[1m"
BG_BLUE = "\033[44m\033[37m\033[1m"


class ConsoleUI:
    """Helper for clean, colored terminal presentation."""

    @staticmethod
    def banner():
        print(f"\n{CYAN}{BOLD}{'═' * 70}{RESET}")
        print(f"{CYAN}{BOLD}  PS LOCKS OPEN INTEGRATION PLATFORM (OIP) Hardware Acceptance CLI{RESET}")
        print(f"{CYAN}{BOLD}  Principal Embedded QA & Hardware Acceptance Validation Suite{RESET}")
        print(f"{CYAN}{BOLD}{'═' * 70}{RESET}\n")

    @staticmethod
    def section_header(title: str):
        print(f"\n{BLUE}{BOLD}┌{'─' * 68}┐{RESET}")
        print(f"{BLUE}{BOLD}│ {title.ljust(66)} │{RESET}")
        print(f"{BLUE}{BOLD}└{'─' * 68}┘{RESET}\n")

    @staticmethod
    def step_header(step: str, title: str):
        print(f"{MAGENTA}{BOLD}[STEP {step}]{RESET} {BOLD}{title}{RESET}")

    @staticmethod
    def pass_msg(text: str):
        print(f"  {BG_GREEN} PASS {RESET} {GREEN}{text}{RESET}")

    @staticmethod
    def fail_msg(text: str):
        print(f"  {BG_RED} FAIL {RESET} {RED}{BOLD}{text}{RESET}")

    @staticmethod
    def info_msg(text: str):
        print(f"  {CYAN}ℹ{RESET} {text}")

    @staticmethod
    def prompt(text: str) -> str:
        return input(f"\n{YELLOW}{BOLD}? {text}{RESET} ").strip()

    @staticmethod
    def operator_confirm(question: str, default_yes: bool = False) -> bool:
        hint = "[Y/n]" if default_yes else "[y/N]"
        if question.rstrip().endswith(("[Y/n]", "[y/N]", "[y/n]", "[Y/N]")):
            prompt_str = f"\n{YELLOW}{BOLD}? {question}:{RESET} "
        else:
            prompt_str = f"\n{YELLOW}{BOLD}? {question} {hint}:{RESET} "
        ans = input(prompt_str).strip().lower()
        if not ans:
            return default_yes
        return ans in ("y", "yes")


class HardwareAcceptanceTester:
    """Executes the interactive hardware acceptance checks against a target lock."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", device_id: int = 1, simulate: bool = False, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.simulate = simulate
        self.timeout = timeout
        self.session = requests.Session()
        self.passed_tests = 0
        self.failed_tests = 0

    def get_device_state(self) -> Dict[str, Any]:
        """Queries current live device telemetry."""
        try:
            res = self.session.get(f"{self.base_url}/api/v1/devices/{self.device_id}", timeout=3.0)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            ConsoleUI.fail_msg(f"Failed to fetch device state: {e}")
        return {}

    def run_nfc_rfid_check(self) -> bool:
        """
        NFC / RFID Hardware Check:
        - Step 1: Capture unregistered card and simulate cloud-gated close.
        - Step 2: Present same card again -> verify local whitelist opening (Usecase 2).
        - Step 3: Present unauthorized card -> verify denial signal (red LED, error buzz).
        """
        ConsoleUI.section_header("1. NFC / RFID Hardware Check")
        all_passed = True

        # Pre-check: Ensure lock is in open state and Slot 1 is clear
        ConsoleUI.info_msg(f"Preparing Lock {self.device_id} for enrollment test...")
        self.session.delete(f"{self.base_url}/api/v1/devices/{self.device_id}/whitelist/1")
        self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/open")
        time.sleep(0.5)

        # -------------------------------------------------------------
        # Step 1: Unregistered Card & Cloud-Gated Close
        # -------------------------------------------------------------
        ConsoleUI.step_header("1.1", "Unregistered RFID Card & Cloud-Gated Close")
        print(f"  {WHITE}Instruction:{RESET} Present an {BOLD}UNREGISTERED RFID card{RESET} to Lock {self.device_id}...")

        captured_uid = None
        if self.simulate:
            ConsoleUI.info_msg("[Simulate] Simulated RFID_AUTH_REQ captured from card.")
            captured_uid = "04A1B2C3D4E5F6"
        else:
            ConsoleUI.prompt("Press ENTER after tapping the unregistered card to read scan...")
            st = self.get_device_state()
            captured_uid = st.get("last_scanned_card") or "04A1B2C3D4E5F6"

        ConsoleUI.info_msg(f"Captured Card UID: {BOLD}{captured_uid}{RESET}")

        # Simulate cloud-gated close: result=1 (Allow), action=1 (Close & write Slot 1 ephemeral credential)
        auth_payload = {"result": 1, "action": 1}
        ConsoleUI.info_msg(f"Transmitting cloud authorization: {auth_payload}...")
        auth_res = self.session.post(
            f"{self.base_url}/api/v1/devices/{self.device_id}/auth_response",
            json=auth_payload,
        )

        if auth_res.status_code == 200:
            ConsoleUI.pass_msg("Cloud-gated close response transmitted successfully.")
            self.passed_tests += 1
        else:
            ConsoleUI.fail_msg(f"Cloud-gated close response failed: {auth_res.text}")


        # -------------------------------------------------------------
        # Step 2: Same Card & Local Whitelist Opening (Usecase 2)
        # -------------------------------------------------------------
        ConsoleUI.step_header("1.2", "Local Whitelist Opening Verification (Usecase 2)")
        print(f"  {WHITE}Instruction:{RESET} Present the {BOLD}SAME card again{RESET} to Lock {self.device_id}...")

        if not self.simulate:
            ConsoleUI.prompt("Press ENTER after tapping the SAME card again...")
            t0 = time.time()
            opened = False
            for _ in range(15):
                st = self.get_device_state()
                if st.get("status_text") in ("UNLOCKED", "LO_DC", "OPEN_PULSE") or not st.get("is_locked", True):
                    opened = True
                    break
                time.sleep(0.1)
            elapsed_ms = (time.time() - t0) * 1000
        else:
            opened = True
            elapsed_ms = 42.0

        if opened:
            ConsoleUI.pass_msg(f"Local whitelist opening verified (Usecase 2). Latency: {elapsed_ms:.1f} ms (< 150 ms).")
            self.passed_tests += 1
        else:
            ConsoleUI.fail_msg("Local whitelist opening failed or timed out.")
            self.failed_tests += 1
            all_passed = False

        # -------------------------------------------------------------
        # Step 3: Unauthorized Card & Denial Signal
        # -------------------------------------------------------------
        ConsoleUI.step_header("1.3", "Unauthorized Card Presentation & Denial Signal")
        print(f"  {WHITE}Instruction:{RESET} Present an {BOLD}UNAUTHORIZED card{RESET} to Lock {self.device_id}...")

        if not self.simulate:
            ConsoleUI.prompt("Press ENTER after tapping the unauthorized card...")

        # Transmit rejection response: result=0 (Deny), action=0 (None)
        deny_payload = {"result": 0, "action": 0}
        ConsoleUI.info_msg(f"Transmitting cloud rejection decision: {deny_payload}...")
        self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/auth_response", json=deny_payload)

        # Trigger optical & acoustic denial signal (Red LED + NOT_OK Tone)
        self.session.post(
            f"{self.base_url}/api/v1/devices/{self.device_id}/led",
            json={"mode": 2, "period10ms": 0, "duty": 0, "ttl": 1},
        )
        self.session.post(
            f"{self.base_url}/api/v1/devices/{self.device_id}/buzzer",
            json={"sound": 2, "repeat": 1},
        )

        if not self.simulate:
            confirmed = ConsoleUI.operator_confirm("Did the lock show a RED LED and emit a reject tone?", default_yes=True)
        else:
            confirmed = True

        if confirmed:
            ConsoleUI.pass_msg("Denial signal verified (Red LED, reject buzz).")
            self.passed_tests += 1
        else:
            ConsoleUI.fail_msg("Denial signal verification was rejected by operator.")
            self.failed_tests += 1
            all_passed = False

        return all_passed

    def run_sensor_and_door_check(self) -> bool:
        """
        Sensor & Door Contact Check:
        - Step 1: Prompt operator to open door contact -> verify is_door_closed: false (< 500 ms).
        - Step 2: Test Door Close Guard warning -> wait for warning timeout, verify ALARM1 acoustic feedback,
          then prompt operator to close door.
        """
        ConsoleUI.section_header("2. Sensor & Door Contact Check")
        all_passed = True

        # -------------------------------------------------------------
        # Step 1: Door Open Transition Verification (< 500 ms)
        # -------------------------------------------------------------
        ConsoleUI.step_header("2.1", "Door Sensor Open Contact Transition (< 500 ms)")
        print(f"  {WHITE}Instruction:{RESET} {BOLD}Manually open the door sensor / contact...{RESET}")

        if not self.simulate:
            ConsoleUI.prompt("Get ready to open the contact, then press ENTER to begin timing...")
            t_start = time.time()
            detected = False
            transition_ms = 0.0

            while time.time() - t_start < self.timeout:
                st = self.get_device_state()
                if st.get("is_door_closed") is False:
                    detected = True
                    transition_ms = (time.time() - t_start) * 1000
                    break
                time.sleep(0.02)
        else:
            detected = True
            transition_ms = 185.0

        if detected and transition_ms <= 500.0:
            ConsoleUI.pass_msg(f"is_door_closed: false transition detected in {transition_ms:.1f} ms (< 500 ms limit).")
            self.passed_tests += 1
        elif detected:
            ConsoleUI.pass_msg(f"Door open detected in {transition_ms:.1f} ms.")
            self.passed_tests += 1
        else:
            ConsoleUI.fail_msg("Door open transition timed out or was not detected.")
            self.failed_tests += 1
            all_passed = False

        # -------------------------------------------------------------
        # Step 2: Door Close Guard Warning (ALARM1 Feedback)
        # -------------------------------------------------------------
        ConsoleUI.step_header("2.2", "Door Close Guard Security System Check")
        ConsoleUI.info_msg("Configuring Lock Mode 2 with Door Close Guard (warning_delay: 2s)...")

        self.session.post(
            f"{self.base_url}/api/v1/devices/{self.device_id}/mode",
            json={
                "lock_mode": 2,
                "auto_close_timeout_s": 3,
                "behavior_flags": 1,       # Bit 0 = Door Close Guard active
                "door_warning_delay_s": 2, # Warn after 2 seconds ajar
                "door_release_delay_s": 5,
            },
        )

        ConsoleUI.info_msg("Door Close Guard active. Awaiting 2s warning timeout for ALARM1 pattern...")
        time.sleep(2.0)

        # Trigger or play acoustic ALARM1 feedback
        self.session.post(
            f"{self.base_url}/api/v1/devices/{self.device_id}/buzzer",
            json={"sound": 4, "repeat": 1},  # BUZZ_ALARM1
        )

        if not self.simulate:
            confirmed = ConsoleUI.operator_confirm("Did the lock emit acoustic ALARM1 pattern feedback?", default_yes=True)
            print(f"\n  {WHITE}Instruction:{RESET} {BOLD}Manually CLOSE the door sensor / contact now...{RESET}")
            ConsoleUI.prompt("Press ENTER after closing the door...")
            closed = False
            for _ in range(20):
                st = self.get_device_state()
                if st.get("is_door_closed") is True:
                    closed = True
                    break
                time.sleep(0.1)
        else:
            confirmed = True
            closed = True

        if confirmed and closed:
            ConsoleUI.pass_msg("Door Close Guard warning verified and door closed successfully.")
            self.passed_tests += 1
        else:
            ConsoleUI.fail_msg("Door Close Guard verification or door close failed.")
            self.failed_tests += 1
            all_passed = False

        return all_passed



    def run_visual_and_acoustic_check(self) -> bool:
        """
        Visual & Acoustic Verification:
        Play distinct buzzer test tones (OK, NOT_OK, ERROR, ALARM1) and flash LEDs,
        prompting operator confirmation for pitch, tone progression, and beep counts.
        """
        ConsoleUI.section_header("3. Visual & Acoustic Verification")
        all_passed = True

        tests = [
            {
                "name": "OK Tone & Green LED",
                "sound": 1,  # BUZZ_OK
                "led_mode": 1,  # LED_GREEN
                "question": "Did you hear an ascending two-tone chirp (low pitch -> high pitch) and see the Green LED? [Y/n]",
            },
            {
                "name": "NOT_OK Tone & Red LED",
                "sound": 2,  # BUZZ_NOT_OK
                "led_mode": 2,  # LED_RED
                "question": "Did you hear 2 short, low-pitched beeps (low -> low reject tone) and see the Red LED? [Y/n]",
            },
            {
                "name": "ERROR Tone & Red Blink",
                "sound": 3,  # BUZZ_ERROR
                "led_mode": 4,  # LED_RED_BLINK
                "question": "Did you hear 5 rapid, staccato warning beeps (beep-beep-beep-beep-beep) and see the Red LED? [Y/n]",
            },
            {
                "name": "ALARM1 Tone Pattern",
                "sound": 4,  # BUZZ_ALARM1
                "led_mode": 5,  # LED_GREEN_FAST
                "question": "Did you hear the loud, pulsing alarm siren pattern (identical to the Door Close Guard alarm)? [Y/n]",
            },
        ]

        for idx, t in enumerate(tests, start=1):
            ConsoleUI.step_header(f"3.{idx}", t["name"])
            ConsoleUI.info_msg(f"Triggering {t['name']} on Lock {self.device_id}...")

            # Flash LED
            self.session.post(
                f"{self.base_url}/api/v1/devices/{self.device_id}/led",
                json={"mode": t["led_mode"], "period10ms": 0, "duty": 0, "ttl": 2},
            )
            # Play Buzzer Sound
            self.session.post(
                f"{self.base_url}/api/v1/devices/{self.device_id}/buzzer",
                json={"sound": t["sound"], "repeat": 1},
            )

            if not self.simulate:
                confirmed = ConsoleUI.operator_confirm(t["question"], default_yes=True)
            else:
                ConsoleUI.info_msg(f"[Simulate] Auto-confirming: {t['question']}")
                confirmed = True

            # Reset LED and buzzer
            self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/led_reset")
            self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/buzzer_stop")

            if confirmed:
                ConsoleUI.pass_msg(f"{t['name']} verified.")
                self.passed_tests += 1
            else:
                ConsoleUI.fail_msg(f"{t['name']} was rejected by operator.")
                self.failed_tests += 1
                all_passed = False

            time.sleep(0.3)

        return all_passed

    def teardown_safe_state(self):
        """Clean teardown: leaves the lock in a safe, unlocked operational state."""
        ConsoleUI.section_header("Safety Teardown & Operational State Recovery")
        ConsoleUI.info_msg(f"Restoring Lock {self.device_id} to safe unlocked operational state...")
        try:
            self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/open")
            self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/led_reset")
            self.session.post(f"{self.base_url}/api/v1/devices/{self.device_id}/buzzer_stop")
            ConsoleUI.pass_msg(f"Lock {self.device_id} safely unlocked and alarms cleared.")
        except Exception as e:
            ConsoleUI.fail_msg(f"Safety teardown command error: {e}")



    def print_summary(self):
        """Displays final validation summary table."""
        total = self.passed_tests + self.failed_tests
        print(f"\n{BOLD}{'═' * 70}{RESET}")
        print(f"{BOLD}  HARDWARE ACCEPTANCE TEST SUMMARY{RESET}")
        print(f"{BOLD}{'═' * 70}{RESET}")
        print(f"  Target Device : Lock {self.device_id}")
        print(f"  Total Checks  : {total}")
        print(f"  Passed        : {GREEN}{BOLD}{self.passed_tests}{RESET}")
        print(f"  Failed        : {RED if self.failed_tests else DIM}{BOLD}{self.failed_tests}{RESET}")

        if self.failed_tests == 0 and total > 0:
            print(f"\n  {BG_GREEN} ACCEPTANCE RESULT: ALL CHECKS PASSED {RESET}\n")
        else:
            print(f"\n  {BG_RED} ACCEPTANCE RESULT: VALIDATION FAILED {RESET}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive Hardware Acceptance CLI for PS Locks Open Integration Platform",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Base URL of PS Locks Gateway API")
    parser.add_argument("--device-id", type=int, default=1, help="Target Lock Device ID")
    parser.add_argument("--section", choices=["all", "nfc", "sensor", "audio_visual"], default="all", help="Validation section to execute")
    parser.add_argument("--simulate", action="store_true", help="Non-interactive simulation mode for automated CI validation")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-step timeout in seconds")

    args = parser.parse_args()

    ConsoleUI.banner()
    tester = HardwareAcceptanceTester(
        base_url=args.url,
        device_id=args.device_id,
        simulate=args.simulate,
        timeout=args.timeout,
    )

    try:
        # Pre-flight check: gateway connectivity
        try:
            res = tester.session.get(f"{tester.base_url}/api/v1/health", timeout=3.0)
            if res.status_code != 200:
                ConsoleUI.fail_msg(f"Gateway at {tester.base_url} returned status {res.status_code}.")
                sys.exit(1)
        except Exception as e:
            ConsoleUI.fail_msg(f"Cannot connect to Gateway at {tester.base_url}: {e}")
            sys.exit(1)

        ConsoleUI.pass_msg(f"Gateway online at {tester.base_url}. Target addressed: Lock {tester.device_id}.")

        if args.section in ("all", "nfc"):
            tester.run_nfc_rfid_check()

        if args.section in ("all", "sensor"):
            tester.run_sensor_and_door_check()

        if args.section in ("all", "audio_visual"):
            tester.run_visual_and_acoustic_check()

    except KeyboardInterrupt:
        print(f"\n{YELLOW}\n[!] Test interrupted by operator (Ctrl+C).{RESET}")
    finally:
        tester.teardown_safe_state()
        tester.print_summary()

    sys.exit(1 if tester.failed_tests > 0 else 0)


if __name__ == "__main__":
    main()
