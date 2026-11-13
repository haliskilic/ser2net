# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

DISTUTILS_USE_PEP517=setuptools
PYTHON_COMPAT=( python3_{12..15} )

inherit distutils-r1 optfeature systemd

DESCRIPTION="Serial-to-network bridge with a web UI"
HOMEPAGE="https://github.com/haliskilic/ser2netweb"
SRC_URI="https://github.com/haliskilic/${PN}/archive/refs/tags/v${PV}.tar.gz -> ${P}.gh.tar.gz"

# the web UI's static files carry htmx (0BSD) and xterm.js (MIT)
LICENSE="AGPL-3 0BSD MIT"
SLOT="0"
KEYWORDS="~amd64"
IUSE="test"
RESTRICT="!test? ( test )"

RDEPEND="
	acct-group/ser2netweb
	acct-user/ser2netweb
	dev-python/h11[${PYTHON_USEDEP}]
	dev-python/jinja2[${PYTHON_USEDEP}]
	dev-python/markupsafe[${PYTHON_USEDEP}]
	dev-python/psutil[${PYTHON_USEDEP}]
	dev-python/pyserial[${PYTHON_USEDEP}]
	dev-python/pyserial-asyncio-fast[${PYTHON_USEDEP}]
	dev-python/python-multipart[${PYTHON_USEDEP}]
	dev-python/starlette[${PYTHON_USEDEP}]
	dev-python/uvicorn[${PYTHON_USEDEP}]
	dev-python/websockets[${PYTHON_USEDEP}]
"
BDEPEND="
	test? (
		${RDEPEND}
	)
"

python_test() {
	# The whole suite (tests/run_all.py) also compares the pinned versions of the release
	# files; these run on the installed dependencies: the configuration, sign-in and the
	# REST API (they start ser2netweb on 127.0.0.1)
	local -x SER2NETWEB_SKIP_PIN_CHECKS=1
	local t
	for t in test_config_validation test_web_auth test_rest_api; do
		"${EPYTHON}" "tests/${t}.py" || die "tests/${t}.py failed with ${EPYTHON}"
	done
}

python_install_all() {
	distutils-r1_python_install_all
	newinitd "${FILESDIR}/${PN}.initd" "${PN}"
	newconfd "${FILESDIR}/${PN}.confd" "${PN}"
	systemd_dounit "${FILESDIR}/${PN}.service"
	dodoc NOTICE THIRD-PARTY-NOTICES.md
}

pkg_postinst() {
	optfeature "MQTT publishing and Sparkplug B" dev-python/paho-mqtt
	optfeature "LDAP / Active Directory sign-in" dev-python/ldap3
	optfeature "OpenID Connect sign-in" dev-python/authlib
	optfeature "self-signed TLS certificates" dev-python/cryptography
	elog "The web UI listens on 127.0.0.1:8080 at first (OpenRC: ${EROOT}/etc/conf.d/${PN};"
	elog "systemd: systemctl edit ${PN}.service, Environment=SER2NETWEB_BIND_IP=...)."
	elog "Set the first admin password in the browser, or as the service user:"
	elog "  sudo -u ${PN} ${PN} --data-dir /var/lib/${PN} --set-password admin"
}
