PORTNAME=	ser2netweb
DISTVERSIONPREFIX=	v
DISTVERSION=	3.0.1
CATEGORIES=	net comms python

MAINTAINER=	mail@haliskilic.com.tr
COMMENT=	Serial-to-network bridge with a web UI
WWW=		https://github.com/haliskilic/ser2netweb

LICENSE=	AGPLv3 BSD0CLAUSE BSD3CLAUSE MIT
LICENSE_COMB=	multi
LICENSE_FILE_AGPLv3=	${WRKSRC}/LICENSE
LICENSE_FILE_BSD0CLAUSE=	${WRKSRC}/tools/licenses/npm/htmx.org.txt
LICENSE_FILE_MIT=	${WRKSRC}/tools/licenses/npm/xterm.txt

BUILD_DEPENDS=	${PYTHON_PKGNAMEPREFIX}setuptools>=0:devel/py-setuptools@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}wheel>=0:devel/py-wheel@${PY_FLAVOR}
RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}h11>=0:net/py-h11@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}Jinja2>=0:devel/py-Jinja2@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}markupsafe>=0:textproc/py-markupsafe@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}psutil>=0:sysutils/py-psutil@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}pyserial>=0:comms/py-pyserial@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}pyserial-asyncio-fast>=0.16:comms/py-pyserial-asyncio-fast@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}python-multipart>=0:www/py-python-multipart@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}starlette>=0:www/py-starlette@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}uvicorn>=0:www/py-uvicorn@${PY_FLAVOR} \
		${PYTHON_PKGNAMEPREFIX}websockets>=0:devel/py-websockets@${PY_FLAVOR}
TEST_DEPENDS=	${RUN_DEPENDS}

USES=		python
USE_GITHUB=	yes
GH_ACCOUNT=	haliskilic
USE_PYTHON=	autoplist noflavors pep517
USE_RC_SUBR=	ser2netweb

NO_ARCH=	yes

SUB_FILES=	pkg-message

USERS=		ser2netweb
GROUPS=		ser2netweb dialer

OPTIONS_DEFINE=	LDAP MQTT OIDC TLS
OPTIONS_DEFAULT=	MQTT
LDAP_DESC=	LDAP / Active Directory sign-in
MQTT_DESC=	MQTT publishing and Sparkplug B
OIDC_DESC=	OpenID Connect sign-in
TLS_DESC=	Self-signed TLS certificates in-process
LDAP_RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}ldap3>=0:net/py-ldap3@${PY_FLAVOR}
MQTT_RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}paho-mqtt>=0:net/py-paho-mqtt@${PY_FLAVOR}
OIDC_RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}authlib>=0:security/py-authlib@${PY_FLAVOR}
TLS_RUN_DEPENDS=	${PYTHON_PKGNAMEPREFIX}cryptography>=0:security/py-cryptography@${PY_FLAVOR}

# the web UI's /licenses page reads the licence texts from the .dist-info's licenses
# folder, where setuptools 77 and newer put them (the ports' 63 doesn't take PEP 639's
# license and license-files)
_LICENSES=	LICENSE NOTICE COMMERCIAL_LICENSE.md THIRD-PARTY-NOTICES.md \
		tools/licenses/npm/htmx.org.txt tools/licenses/npm/xterm-addon-fit.txt \
		tools/licenses/npm/xterm.txt
_LICENSEDIR=	${PYTHONPREFIX_SITELIBDIR:S;${PREFIX}/;;}/${PORTNAME}-${DISTVERSION}.dist-info/licenses
PLIST_FILES=	${_LICENSES:S;^;${_LICENSEDIR}/;}

# ser2netweb/distro.json (pyproject.toml takes it into the wheel): the distribution
# ser2netweb names in --about, its service's data directory (the default without
# --data-dir) and how an optional package is installed, for the hint about a missing one
post-patch:
	@${REINPLACE_CMD} -e 's|"setuptools>=77"|"setuptools"|' \
		-e 's|^license = "\(.*\)"$$|license = {text = "\1"}|' \
		-e '/^license-files = /,/]$$/d' ${WRKSRC}/pyproject.toml
	@${PRINTF} '%s\n' '{"distribution": "FreeBSD ports", "data_dir": "/var/db/${PORTNAME}", "optional": "pkg install ${PYTHON_PKGNAMEPREFIX}{dist}"}' \
		> ${WRKSRC}/ser2netweb/distro.json

post-install:
.for f in ${_LICENSES}
	${MKDIR} ${STAGEDIR}${PREFIX}/${_LICENSEDIR}/${f:H:S;^${f}$;;}
	${INSTALL_DATA} ${WRKSRC}/${f} ${STAGEDIR}${PREFIX}/${_LICENSEDIR}/${f}
.endfor

do-test:
.for t in test_config_validation test_web_auth test_rest_api
	cd ${WRKSRC} && ${SETENV} SER2NETWEB_SKIP_PIN_CHECKS=1 ${PYTHON_CMD} tests/${t}.py
.endfor

.include <bsd.port.mk>
