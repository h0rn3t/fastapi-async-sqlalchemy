import re
from pathlib import Path

from setuptools import setup

with open(Path("fastapi_async_sqlalchemy") / "__init__.py", encoding="utf-8") as fh:
    version = re.search(r'__version__ = "(.*?)"', fh.read(), re.M).group(1)

with open("README.md", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="fastapi-async-sqlalchemy",
    version=version,
    url="https://github.com/h0rn3t/fastapi-async-sqlalchemy.git",
    project_urls={
        "Code": "https://github.com/h0rn3t/fastapi-async-sqlalchemy",
        "Issue tracker": "https://github.com/h0rn3t/fastapi-async-sqlalchemy/issues",
    },
    license="MIT",
    author="Eugene Shershen",
    author_email="h0rn3t.null@gmail.com",
    description="SQLAlchemy middleware for FastAPI",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=["fastapi_async_sqlalchemy"],
    package_data={"fastapi_async_sqlalchemy": ["py.typed"]},
    zip_safe=False,
    python_requires=">=3.7",
    install_requires=["starlette>=0.13.6", "SQLAlchemy>=1.4.9"],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Web Environment",
        "Framework :: AsyncIO",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: Implementation :: CPython",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
