#!/usr/bin/env python3
"""
CODAR Oceanography HF Radar Example
Monitor coastal ocean current radars (CODAR/WERA systems)

CODAR Requirements:
- HF frequencies (3-30 MHz typical)
- Narrow bandwidth (few kHz, FMCW chirp)
- I/Q samples for Doppler processing
- Multiple frequencies for different ranges

This demonstrates setting up for oceanographic radar monitoring.
"""

from ka9q import RadiodControl
import time

def create_codar_channels(control, site_config):
    """
    Create CODAR monitoring channels for an oceanographic radar site
    
    Args:
        control: RadiodControl instance
        site_config: Dict with 'name', 'frequencies', 'bandwidth'
    """
    site_name = site_config['name']
    frequencies = site_config['frequencies']
    bandwidth = site_config.get('bandwidth', 20000)  # 20 kHz default
    
    print(f"Setting up CODAR monitoring for: {site_name}")
    print(f"Frequencies: {[f/1e6 for f in frequencies]} MHz")
    print(f"Bandwidth: {bandwidth/1e3} kHz")
    print()
    
    for freq_hz in frequencies:
        ssrc = int(freq_hz)
        
        print(f"  Creating {freq_hz/1e6:.3f} MHz channel...")
        
        control.create_channel(
            ssrc=ssrc,
            frequency_hz=freq_hz,
            preset="iq",              # I/Q for FMCW processing
            sample_rate=bandwidth,    # Match radar bandwidth
            agc_enable=0,             # Fixed gain
            gain=0.0
        )
        
        time.sleep(0.2)
    
    print(f"\n✓ CODAR channels ready for {site_name}")

def main():
    control = RadiodControl("bee1-hf-status.local", client_id="codar-oceanography")
    
    # Example CODAR site configurations
    # (Real frequencies would come from CODAR site database)
    
    codar_sites = [
        {
            'name': 'Montauk Point, NY',
            'frequencies': [13.47e6, 25.40e6],  # Example frequencies
            'bandwidth': 20000
        },
        {
            'name': 'Bodega Bay, CA',
            'frequencies': [4.46e6, 13.45e6, 25.35e6],
            'bandwidth': 25000
        }
    ]
    
    for site in codar_sites:
        create_codar_channels(control, site)
        print()
    
    print("=" * 60)
    print("CODAR Monitoring Active")
    print("=" * 60)
    print("\nNext steps for ocean current mapping:")
    print("1. Receive RTP I/Q streams")
    print("2. Apply FMCW chirp correlation")
    print("3. Compute Doppler spectra")
    print("4. Extract radial velocities")
    print("5. Combine multi-site data for 2D current maps")
    print("\nRTP SSRCs:", [int(f) for site in codar_sites for f in site['frequencies']])

if __name__ == '__main__':
    main()
