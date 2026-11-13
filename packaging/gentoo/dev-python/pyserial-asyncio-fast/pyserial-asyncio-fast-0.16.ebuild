# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

DISTUTILS_USE_PEP517=setuptools
PYTHON_COMPAT=( python3_{12..15} )

inherit distutils-r1 pypi

DESCRIPTION="Asynchronous I/O for pySerial (fast fork of pyserial-asyncio)"
HOMEPAGE="
	https://github.com/home-assistant-libs/pyserial-asyncio-fast/
	https://pypi.org/project/pyserial-asyncio-fast/
"

LICENSE="BSD"
SLOT="0"
KEYWORDS="~amd64"

RDEPEND="
	dev-python/pyserial[${PYTHON_USEDEP}]
"

PATCHES=(
	"${FILESDIR}/${P}-py314-event-loop.patch"
)

distutils_enable_tests pytest
