# ser2netweb for Fedora and EPEL, kept in packaging/fedora/ of
# https://github.com/haliskilic/ser2netweb (CI builds it in Fedora and AlmaLinux containers:
# .github/workflows/distro-packages.yml; tools/bump_recipes.py moves it to a release).

# the browser libraries in ser2netweb/web/static/ (vendor/npm/package.json); their npm
# releases are the sources below, for the SRPM (the static files are those releases' own)
%global htmx_version 2.0.11
%global xterm_version 6.0.0
%global xterm_fit_version 0.11.0

Name:           ser2netweb
Version:        3.1.0
Release:        1%{?dist}
Summary:        Serial-to-network bridge with a web UI

# ser2netweb: AGPL-3.0-only; copied code of pyserial-asyncio-fast and Starlette:
# BSD-3-Clause; xterm.js and its fit addon: MIT; htmx: 0BSD
License:        AGPL-3.0-only AND BSD-3-Clause AND MIT AND 0BSD
URL:            https://github.com/haliskilic/ser2netweb
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
Source1:        ser2netweb.service
Source2:        ser2netweb.sysusers
Source3:        ser2netweb.tmpfiles
Source4:        https://registry.npmjs.org/htmx.org/-/htmx.org-%{htmx_version}.tgz
Source5:        https://registry.npmjs.org/@xterm/xterm/-/xterm-%{xterm_version}.tgz
Source6:        https://registry.npmjs.org/@xterm/addon-fit/-/addon-fit-%{xterm_fit_version}.tgz

BuildArch:      noarch
# RHEL 10 (EPEL 10) has setuptools 69, which doesn't know PEP 639's licence fields (%%prep)
%if 0%{?rhel} && 0%{?rhel} <= 10
%bcond_without old_setuptools
%else
%bcond_with old_setuptools
%endif
BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros
%{?sysusers_requires_compat}

# the optional features (each says in the UI what is missing without its package)
Recommends:     python3dist(paho-mqtt) >= 1.6.1
Recommends:     python3dist(ldap3) >= 2.9.1
Recommends:     python3dist(authlib) >= 1.4.0
Recommends:     python3dist(cryptography) >= 43.0.0

Provides:       bundled(js-htmx) = %{htmx_version}
Provides:       bundled(js-xterm) = %{xterm_version}
Provides:       bundled(js-xterm-addon-fit) = %{xterm_fit_version}
# ser2netweb/engine/serial_transport.py, used as pyserial-asyncio-fast isn't packaged
Provides:       bundled(python3dist(pyserial-asyncio-fast)) = 0.16

%description
ser2netweb shares local serial ports over the network as raw TCP, telnet,
RFC 2217 or UDP, is a Modbus RTU/TCP gateway and publishes serial data and
Modbus points to MQTT and Sparkplug B. Everything is set up in a web UI with
accounts, roles, two-factor sign-in, an audit log, a REST API and a cluster
view of several machines; a web console shows a port's traffic live.

The package installs a hardened service unit, which runs it as the system
account ser2netweb with its data in /var/lib/ser2netweb; it is not enabled by
default. ser2netweb is an independent project, not related to the ser2net
daemon (package ser2net), whose configuration files it can import.

%prep
%autosetup -p1
# not packaged in Fedora: ser2netweb runs on its built-in copy of that library's
# serial transport (ser2netweb/engine/serial_transport.py)
sed -i '/"pyserial-asyncio-fast>=/d' pyproject.toml
%if %{with old_setuptools}
# RHEL 10's python3-setuptools is 69, older than PEP 639's licence fields in
# pyproject.toml (setuptools 77): the build takes them out (the licence is the License
# tag above) and %%install puts the licence texts where setuptools 77 would
sed -i -e 's|"setuptools>=77"|"setuptools"|' \
    -e 's|^license = "\(.*\)"$|license = {text = "\1"}|' \
    -e '/^license-files = /,/]$/d' pyproject.toml
%endif
# ser2netweb/distro.json (pyproject.toml takes it into the wheel): the distribution
# ser2netweb names in --about, its service's data directory (the default without
# --data-dir) and how an optional package is installed, for the hint about a missing one
cat >ser2netweb/distro.json <<'EOF'
{"distribution": "%{?fedora:Fedora}%{?rhel:EPEL}", "data_dir": "%{_sharedstatedir}/ser2netweb", "optional": "sudo dnf install python3-{dist}"}
EOF

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%if %{with old_setuptools}
# the .dist-info's licenses/ folder, where the web UI's /licenses page reads them
licenses=%{buildroot}%{python3_sitelib}/ser2netweb-%{version}.dist-info/licenses
install -Dpm 0644 -t $licenses LICENSE NOTICE COMMERCIAL_LICENSE.md THIRD-PARTY-NOTICES.md
install -Dpm 0644 -t $licenses/tools/licenses/npm tools/licenses/npm/*.txt
%endif
%pyproject_save_files -l ser2netweb
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_unitdir}/ser2netweb.service
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_sysusersdir}/ser2netweb.conf
install -Dpm 0644 %{SOURCE3} %{buildroot}%{_tmpfilesdir}/ser2netweb.conf
install -dm 0700 %{buildroot}%{_sharedstatedir}/ser2netweb

%check
%pyproject_check_import
# the tests that start no server and open no network socket (each a script)
for t in \
    test_access_policy.py \
    test_clock_check.py \
    test_cluster_peer_save.py \
    test_config_types.py \
    test_config_validation.py \
    test_console_log.py \
    test_dependency_internals.py \
    test_env_bind.py \
    test_explain.py \
    test_kept_names.py \
    test_live_table.py \
    test_log_maintenance.py \
    test_log_read.py \
    test_log_rotation.py \
    test_login_throttle.py \
    test_mapping_form_roundtrip.py \
    test_mixed_version_edit.py \
    test_modbus_frame.py \
    test_modbus_write.py \
    test_password_hashing.py \
    test_protocol_commands.py \
    test_public_url.py \
    test_retention.py \
    test_rfc2217_readonly.py \
    test_rfc2217_settings.py \
    test_secure_cookies.py \
    test_ser2net_import.py \
    test_serial_compat.py \
    test_serial_mqtt.py \
    test_sparkplug.py \
    test_sparkplug_node.py \
    test_starlette_compat.py \
    test_text_fold.py \
    test_time_stamps.py \
    test_traffic_counters.py \
    test_trusted_proxies.py \
    test_ui_contrast.py \
    test_ui_markup.py \
    ; do
    PYTHONDONTWRITEBYTECODE=1 %{python3} tests/$t
done

%pre
%{?sysusers_create_compat:%sysusers_create_compat %{SOURCE2}}

%post
%systemd_post ser2netweb.service

%preun
%systemd_preun ser2netweb.service

%postun
%systemd_postun_with_restart ser2netweb.service

%files -f %{pyproject_files}
%if %{with old_setuptools}
%license %{python3_sitelib}/ser2netweb-%{version}.dist-info/licenses/
%endif
%doc README.en.md README.md CHANGELOG.md SECURITY.md
%doc docs/LINUX-SERIAL.md docs/MIGRATING-FROM-SER2NET.md docs/MODBUS-MQTT.md
%doc docs/PRIVACY.md docs/REVERSE-PROXY.md docs/SSO.md
%{_bindir}/ser2netweb
%{_unitdir}/ser2netweb.service
%{_sysusersdir}/ser2netweb.conf
%{_tmpfilesdir}/ser2netweb.conf
%attr(0700,ser2netweb,ser2netweb) %dir %{_sharedstatedir}/ser2netweb

%changelog
* Sat Nov 14 2026 Halis KILIÇ <mail@haliskilic.com.tr> - 3.1.0-1
- Update to 3.1.0

* Fri Nov 13 2026 Halis KILIÇ <mail@haliskilic.com.tr> - 3.0.1-1
- Update to 3.0.1

* Fri Nov 13 2026 Halis KILIÇ <mail@haliskilic.com.tr> - 3.0.0-1
- Initial package
