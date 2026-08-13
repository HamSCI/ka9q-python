#!/usr/bin/env python3
"""
SuperDARN Radar Recorder Example
Create channels for SuperDARN coherent scatter radar reception

SuperDARN Requirements:
- I/Q samples (complex baseband)
- Wide bandwidth (50+ kHz for Doppler analysis)
- Multiple frequencies (8-20 MHz typical)
- High time resolution (radar pulses)

This example shows how to set up channels for SuperDARN monitoring
without application-specific assumptions.
"""

from ka9q import RadiodControl
import time

def create_superdarn_channels(control, frequency_list, bandwidth_hz=50000):
    """
    Create SuperDARN monitoring channels
    
    Args:
        control: RadiodControl instance
        frequency_list: List of frequencies in Hz
        bandwidth_hz: Sample rate (default 50 kHz for radar pulses)
    """
    for freq_hz in frequency_list:
        ssrc = int(freq_hz)  # Use frequency as SSRC
        
        print(f"Creating SuperDARN channel: {freq_hz/1e6:.3f} MHz...")
        
        control.create_channel(
            ssrc=ssrc,
            frequency_hz=freq_hz,
            preset="iq",              # I/Q needed for Doppler
            sample_rate=bandwidth_hz,  # Wide bandwidth
            agc_enable=0,             # Fixed gain for radar
            gain=0.0                  # Adjust based on signal strength
        )
        
        time.sleep(0.2)  # Brief delay between channel creations
        
    print(f"\n✓ Created {len(frequency_list)} SuperDARN channels")

def main():
    # Connect to radiod
    control = RadiodControl("bee1-hf-status.local", client_id="superdarn-recorder")
    
    # SuperDARN common frequencies (example - actual depends on radar site)
    superdarn_freqs = [
        8.0e6,   # 8 MHz
        10.0e6,  # 10 MHz
        12.0e6,  # 12 MHz
        14.0e6,  # 14 MHz
        16.0e6,  # 16 MHz
        18.0e6,  # 18 MHz
        20.0e6   # 20 MHz
    ]
    
    create_superdarn_channels(control, superdarn_freqs, bandwidth_hz=50000)
    
    print("\nRTP streams ready for SuperDARN processing")
    print("Next steps:")
    print("1. Receive RTP packets with SSRCs:", [int(f) for f in superdarn_freqs])
    print("2. Apply SuperDARN pulse detection")
    print("3. Compute Doppler spectra")
    print("4. Generate ionospheric maps")

if __name__ == '__main__':
    main()
