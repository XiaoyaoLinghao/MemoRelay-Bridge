"""Build hook: package the vendored contract files inside the DMA wheel."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithContracts(build_py):
    def run(self) -> None:
        super().run()
        source = Path(__file__).resolve().parent / "contracts" / "v1"
        target = Path(self.build_lib) / "dma" / "contracts_v1"
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for path in source.iterdir():
                if path.is_file():
                    shutil.copy2(path, target / path.name)


setup(cmdclass={"build_py": BuildPyWithContracts})
