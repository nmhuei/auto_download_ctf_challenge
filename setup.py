from setuptools import find_namespace_packages, setup
from setuptools.command.build_py import build_py as _build_py


class ForceBuildPy(_build_py):
    """Never trust stale build/lib timestamps when producing a wheel.

    Direct PEP 517 wheel builds may reuse an existing build/lib tree. If a
    stale/truncated artifact there has a newer mtime than the real source,
    setuptools' incremental build can package the stale file unchanged. Force
    every package module to be recopied from source for reproducible wheels.
    """

    def finalize_options(self):
        super().finalize_options()
        self.force = True


setup(
    name="ctf-toolkit",
    version="3.0.0",
    description="Unified CTF Challenge Downloader, Submitter, Container Manager & Dashboard",
    author="Antigravity",
    packages=find_namespace_packages(include=["ctf_downloader*"]),
    cmdclass={"build_py": ForceBuildPy},
    data_files=[
        ("share/bash-completion/completions", ["completions/ctf.bash"]),
        ("share/zsh/site-functions", ["completions/ctf.zsh"]),
    ],
    install_requires=[
        "requests>=2.28.0",
        "beautifulsoup4>=4.11.0",
        "rich>=13.0.0",
        "gdown>=4.7.0",
        "urllib3>=1.26.0",
        "cryptography>=41.0.0",
        "curl_cffi>=0.7.4"
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
