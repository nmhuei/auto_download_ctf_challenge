from setuptools import setup, find_packages

setup(
    name="ctf-toolkit",
    version="3.0.0",
    description="Unified CTF Challenge Downloader, Submitter, Container Manager & Dashboard",
    author="Antigravity",
    packages=find_packages(),
    data_files=[
        ("share/bash-completion/completions", ["completions/ctf.bash"]),
        ("share/zsh/site-functions", ["completions/ctf.zsh"]),
    ],
    install_requires=[
        "requests>=2.28.0",
        "beautifulsoup4>=4.11.0",
        "rich>=13.0.0",
        "gdown>=4.7.0",
        "urllib3>=1.26.0"
    ],
    entry_points={
        "console_scripts": [
            "ctf=ctf_downloader.cli:main",
            "ctfcli=ctf_downloader.cli:main",
            "ctf-tool=ctf_downloader.cli:main"
        ]
    },
    python_requires=">=3.8",
)
