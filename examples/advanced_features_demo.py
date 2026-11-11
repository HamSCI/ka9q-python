#!/usr/bin/env python3
"""
Demonstration of newly exposed radiod features in ka9q-python

This script demonstrates the use of advanced radiod features that
were previously not exposed in the Python interface.
"""

from ka9q.control import RadiodControl
from ka9q.types import Encoding

def main():
    # Connect to radiod
    control = RadiodControl(control_address="239.101.6.1")
    
    # Example SSRC (replace with your actual channel SSRC)
    ssrc = 12345
    
    print("=== Advanced RadioD Feature Demonstrations ===\n")
    
    # 1. Linear sweep (dechirp), replaces the retired Doppler command
    print("1. Setting a linear sweep (dechirp)")
    control.set_sweep(ssrc=ssrc, rate_hz_per_s=-25733.913, period_s=1.0)
    print("   ✓ Sweep: -25733.913 Hz/s, period 1 s\n")
    
    # 2. PLL configuration for carrier tracking
    print("2. Configuring PLL for coherent AM detection")
    control.set_pll(ssrc=ssrc, enable=True, bandwidth_hz=50, square=False)
    print("   ✓ PLL enabled with 50 Hz bandwidth\n")
    
    # 3. SNR squelch
    print("3. Setting SNR-based squelch")
    control.set_squelch(ssrc=ssrc, enable=True, open_snr_db=10, close_snr_db=8)
    print("   ✓ Squelch opens at 10 dB, closes at 8 dB\n")
    
    # 4. Independent Sideband mode
    print("4. Enabling Independent Sideband (ISB) mode")
    control.set_independent_sideband(ssrc=ssrc, enable=True)
    control.set_output_channels(ssrc=ssrc, channels=2)
    print("   ✓ ISB mode enabled (USB/LSB to L/R channels)\n")
    
    # 5. Secondary filter configuration
    print("5. Configuring secondary filter for extra selectivity")
    control.set_filter2(ssrc=ssrc, blocksize=5, kaiser_beta=3.5)
    print("   ✓ Filter2 configured with blocksize=5, beta=3.5\n")
    
    # 6. Opus encoder settings
    print("6. Setting Opus encoding and bitrate")
    control.set_output_encoding(ssrc=ssrc, encoding=Encoding.OPUS)
    control.set_opus_bitrate(ssrc=ssrc, bitrate=64000)
    print("   ✓ Opus encoding at 64 kbps\n")
    
    # 7. Spectrum analyzer mode
    print("7. Configuring spectrum analyzer mode")
    control.set_spectrum(ssrc=ssrc, bin_bw_hz=100, bin_count=512, kaiser_beta=6.0)
    print("   ✓ Spectrum mode: 100 Hz bins, 512 bins, beta=6.0\n")
    
    # 8. Packet buffering control
    print("8. Setting packet buffering for lower packet rate")
    control.set_packet_buffering(ssrc=ssrc, min_blocks=2)
    print("   ✓ Minimum 2 blocks (40ms) buffering\n")
    
    # 9. Status interval configuration
    print("9. Configuring automatic status reporting")
    control.set_status_interval(ssrc=ssrc, interval=50)
    print("   ✓ Status sent every 50 frames\n")
    
    # 10. RF hardware controls (hardware-dependent)
    print("10. Adjusting RF gain and attenuation")
    # control.set_rf_gain(ssrc=ssrc, gain_db=20)
    # control.set_rf_attenuation(ssrc=ssrc, atten_db=10)
    print("   ✓ RF controls available (commented out - hardware dependent)\n")
    
    # 11. AGC threshold (separate from existing AGC settings)
    print("11. Setting AGC threshold")
    control.set_agc_threshold(ssrc=ssrc, threshold_db=10)
    print("   ✓ AGC threshold at 10 dB above noise\n")
    
    # 12. First LO tuning (affects all channels - use with care!)
    print("12. Setting first LO frequency")
    # control.set_first_lo(ssrc=ssrc, frequency_hz=14.1e6)
    print("   ✓ First LO tuning available (commented out - affects all channels)\n")
    
    # 13. FM-specific features
    print("13. FM threshold extension (for weak signals)")
    control.set_fm_threshold_extension(ssrc=ssrc, enable=True)
    print("   ✓ FM threshold extension enabled\n")
    
    # 14. Linear mode envelope detection
    print("14. Enabling envelope detection (for AM)")
    control.set_envelope_detection(ssrc=ssrc, enable=True)
    print("   ✓ Envelope detection enabled\n")
    
    # 15. Demodulator type switching
    print("15. Changing demodulator type")
    # control.set_demod_type(ssrc=ssrc, demod_type=1)  # 0=LINEAR, 1=FM, 2=WFM, 3=SPECTRUM
    print("   ✓ Demod type switching available (commented out)\n")
    
    # 16. Output destination
    print("16. Setting RTP output destination")
    # control.set_destination(ssrc=ssrc, address="239.1.2.3", port=5004)
    print("   ✓ Destination control available (commented out)\n")
    
    # 17. Option bits (experimental/debug)
    print("17. Setting option bits")
    # control.set_options(ssrc=ssrc, set_bits=0x01, clear_bits=0x02)
    print("   ✓ Option bit control available (commented out - experimental)\n")
    
    print("\n=== All features demonstrated! ===")
    print("Note: Some features are commented out to prevent unintended changes.")
    print("Uncomment and adjust as needed for your specific use case.")
    
    control.close()

if __name__ == "__main__":
    main()
