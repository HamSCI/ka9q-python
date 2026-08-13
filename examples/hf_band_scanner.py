#!/usr/bin/env python3
"""
HF Band Scanner Example
Scan HF bands for activity - completely different use case than recording

Demonstrates:
- Dynamic channel creation/deletion
- Arbitrary frequency hopping
- No storage assumptions
- User-controlled scan patterns
"""

from ka9q import RadiodControl
import time

class HFScanner:
    """General-purpose HF band scanner using radiod"""
    
    def __init__(self, radiod_address):
        self.control = RadiodControl(radiod_address, client_id="hf-band-scanner")
        self.current_ssrc = None
    
    def scan_frequency(self, freq_hz, mode="usb", dwell_time=2.0):
        """
        Scan a single frequency
        
        Args:
            freq_hz: Frequency in Hz
            mode: Demod mode ("usb", "lsb", "am", "cw")
            dwell_time: How long to listen (seconds)
        """
        # Use a fixed SSRC for scanning (reuse channel)
        ssrc = 99999999
        
        print(f"Scanning {freq_hz/1e6:.3f} MHz ({mode})...")
        
        # Update channel to new frequency
        self.control.create_channel(
            ssrc=ssrc,
            frequency_hz=freq_hz,
            preset=mode,
            sample_rate=12000,
            agc_enable=1,
            gain=0.0
        )
        
        # Dwell on frequency (user would monitor for signals here)
        time.sleep(dwell_time)
    
    def scan_band(self, start_mhz, end_mhz, step_khz=5, mode="usb"):
        """
        Scan an entire band
        
        Args:
            start_mhz: Start frequency in MHz
            end_mhz: End frequency in MHz
            step_khz: Step size in kHz
            mode: Demod mode
        """
        start_hz = start_mhz * 1e6
        end_hz = end_mhz * 1e6
        step_hz = step_khz * 1e3
        
        freq_hz = start_hz
        while freq_hz <= end_hz:
            self.scan_frequency(freq_hz, mode=mode, dwell_time=1.0)
            freq_hz += step_hz
        
        print(f"✓ Scan complete: {start_mhz}-{end_mhz} MHz")

def main():
    scanner = HFScanner("bee1-hf-status.local")
    
    print("HF Band Scanner")
    print("=" * 60)
    print()
    
    # Example scan patterns
    
    # Scan 20m amateur band
    print("Scanning 20m amateur band (14.000-14.350 MHz)...")
    scanner.scan_band(
        start_mhz=14.000,
        end_mhz=14.350,
        step_khz=5,
        mode="usb"
    )
    print()
    
    # Scan AM broadcast band
    print("Scanning AM broadcast band (9.4-9.9 MHz)...")
    scanner.scan_band(
        start_mhz=9.4,
        end_mhz=9.9,
        step_khz=5,
        mode="am"
    )
    print()
    
    # Check specific maritime frequencies
    maritime_freqs = [
        (4.125e6, "usb", "Maritime distress"),
        (8.291e6, "usb", "Maritime working"),
        (12.290e6, "usb", "Maritime working"),
        (16.420e6, "usb", "Maritime working")
    ]
    
    print("Checking maritime frequencies...")
    for freq, mode, desc in maritime_freqs:
        print(f"  {freq/1e6:.3f} MHz - {desc}")
        scanner.scan_frequency(freq, mode=mode, dwell_time=3.0)
    
    print()
    print("=" * 60)
    print("Scan complete!")
    print("\nNote: This example just tunes channels.")
    print("In real use, you'd monitor RTP stream for signals,")
    print("measure SNR, detect activity, log contacts, etc.")

if __name__ == '__main__':
    main()
