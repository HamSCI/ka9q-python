#!/usr/bin/env python3
"""
Simple AM Radio Example
Listen to WWV time signal on 10 MHz

Demonstrates: Minimal code to create a channel and start receiving
"""

from ka9q import RadiodControl

def main():
    # Connect to radiod
    control = RadiodControl("bee1-hf-status.local", client_id="simple-am-radio")
    
    # Create AM channel for WWV 10 MHz
    ssrc = 10000000  # Use frequency as SSRC (convention)
    
    control.create_channel(
        ssrc=ssrc,
        frequency_hz=10.0e6,    # 10 MHz
        preset="am",            # AM demodulation
        sample_rate=12000,      # 12 kHz audio
        agc_enable=1,           # Enable AGC for AM
        gain=0.0                # Not used with AGC
    )
    
    print(f"✓ AM channel created:")
    print(f"  Frequency: 10.0 MHz")
    print(f"  SSRC: {ssrc}")
    print(f"  Sample Rate: 12 kHz")
    print(f"  Mode: AM")
    print(f"\nRTP stream available on radiod's multicast address")
    print(f"Use radiod's audio output or decode RTP stream with SSRC {ssrc}")

if __name__ == '__main__':
    main()
