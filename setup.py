#!/usr/bin/env python3
"""
ka9q-python: Python interface for ka9q-radio control
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read long description from README
readme = Path(__file__).parent / 'README.md'
long_description = readme.read_text() if readme.exists() else ''

setup(
    name='ka9q-python',
    version='3.10.0',
    description='Python interface for ka9q-radio control and monitoring',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Michael Hauan AC0G',
    author_email='ac0g@hauan.org',
    url='https://github.com/HamSCI/ka9q-python',
    packages=find_packages(),
    python_requires='>=3.9',
    install_requires=[
        'numpy>=1.24.0',
    ],
    extras_require={
        'dev': [
            'pytest>=7.0.0',
            'pytest-cov>=4.0.0',
        ],
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Science/Research',
        'Intended Audience :: Telecommunications Industry',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Communications :: Ham Radio',
        'Topic :: Scientific/Engineering',
    ],
    keywords='ka9q-radio sdr ham-radio radio-control',
)
