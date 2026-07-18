from setuptools import setup, find_packages

setup(
    name="lyric_extractor",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "faster-whisper",
        "requests"
    ],
)
