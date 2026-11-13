#
# spec file for package ser2netweb
#
# Copyright (c) 2026 Halis KILIÇ <mail@haliskilic.com.tr>
#
# All modifications and additions to the file contributed by third parties
# remain the property of their copyright owners, unless otherwise agreed
# upon. The license for this file, and modifications and additions to the
# file, is the same license as for the pristine package itself (unless the
# license for the pristine package is not an Open Source License, in which
# case the license is the MIT License). An "Open Source License" is a
# license that conforms to the Open Source Definition (Version 1.9)
# published by the Open Source Initiative.

# Please submit bugfixes or comments via https://github.com/haliskilic/ser2netweb/issues
#
# Kept in packaging/opensuse/ of https://github.com/haliskilic/ser2netweb (CI builds it in
# an openSUSE Tumbleweed container: .github/workflows/distro-packages.yml;
# tools/bump_recipes.py moves it to a release).

# an application, built for the distribution's default Python only
%define pythons python3
Name:           ser2netweb
Version:        3.0.0
Release:        0
Summary:        Serial-to-network bridge with a web UI
# ser2netweb: AGPL-3.0-only; copied code of pyserial-asyncio-fast and Starlette:
# BSD-3-Clause; xterm.js and its fit addon: MIT; htmx: 0BSD
License:        AGPL-3.0-only AND BSD-3-Clause AND MIT AND 0BSD
Group:          Productivity/Networking/Other
URL:            https://github.com/haliskilic/ser2netweb
Source0:        https://github.com/haliskilic/ser2netweb/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
Source1:        ser2netweb.service
Source2:        ser2netweb.sysusers
Source3:        ser2netweb.tmpfiles
BuildRequires:  %{python_module pip}
BuildRequires:  %{python_module setuptools >= 77}
BuildRequires:  %{python_module wheel}
BuildRequires:  fdupes
BuildRequires:  python-rpm-macros
BuildRequires:  sysuser-tools
BuildRequires:  pkgconfig(systemd)
# the tests in %%check
BuildRequires:  %{python_module h11 >= 0.14.0}
BuildRequires:  %{python_module Jinja2 >= 3.1.6}
BuildRequires:  %{python_module MarkupSafe >= 2.1.3}
BuildRequires:  %{python_module psutil >= 5.9.8}
BuildRequires:  %{python_module pyserial >= 3.5}
BuildRequires:  %{python_module python-multipart >= 0.0.20}
BuildRequires:  %{python_module starlette >= 0.46.1}
BuildRequires:  %{python_module uvicorn >= 0.32.0}
BuildRequires:  %{python_module websockets >= 14.2}
# pyserial-asyncio-fast is not in openSUSE: ser2netweb runs on its built-in copy of that
# library's serial transport (ser2netweb/engine/serial_transport.py)
Requires:       python3-h11 >= 0.14.0
Requires:       python3-Jinja2 >= 3.1.6
Requires:       python3-MarkupSafe >= 2.1.3
Requires:       python3-psutil >= 5.9.8
Requires:       python3-pyserial >= 3.5
Requires:       python3-python-multipart >= 0.0.20
Requires:       python3-starlette >= 0.46.1
Requires:       python3-uvicorn >= 0.32.0
Requires:       python3-websockets >= 14.2
# the group of the serial ports the service runs in (SupplementaryGroups=dialout)
Requires:       group(dialout)
# the optional features (each says in the UI what is missing without its package)
Recommends:     python3-Authlib >= 1.4.0
Recommends:     python3-cryptography >= 43.0.0
Recommends:     python3-ldap3 >= 2.9.1
Recommends:     python3-paho-mqtt >= 1.6.1
Provides:       bundled(js-htmx) = 2.0.11
Provides:       bundled(js-xterm) = 6.0.0
Provides:       bundled(js-xterm-addon-fit) = 0.11.0
BuildArch:      noarch
%sysusers_requires
%{?systemd_ordering}

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

%build
%pyproject_wheel
%sysusers_generate_pre %{SOURCE2} ser2netweb ser2netweb.conf

%install
%pyproject_install
%python_expand %fdupes %{buildroot}%{$python_sitelib}
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_unitdir}/ser2netweb.service
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_sysusersdir}/ser2netweb.conf
install -Dpm 0644 %{SOURCE3} %{buildroot}%{_tmpfilesdir}/ser2netweb.conf
install -dm 0700 %{buildroot}%{_sharedstatedir}/ser2netweb

%check
# the tests that start no server and open no network socket (each a script)
for t in \
    test_access_policy.py \
    test_clock_check.py \
    test_cluster_peer_save.py \
    test_config_types.py \
    test_config_validation.py \
    test_console_log.py \
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
    PYTHONDONTWRITEBYTECODE=1 python3 tests/$t
done

%pre -f ser2netweb.pre
%service_add_pre ser2netweb.service

%post
%tmpfiles_create %{_tmpfilesdir}/ser2netweb.conf
%service_add_post ser2netweb.service

%preun
%service_del_preun ser2netweb.service

%postun
%service_del_postun ser2netweb.service

%files
%license LICENSE NOTICE COMMERCIAL_LICENSE.md THIRD-PARTY-NOTICES.md
%doc README.en.md README.md CHANGELOG.md SECURITY.md
%doc docs/LINUX-SERIAL.md docs/MIGRATING-FROM-SER2NET.md docs/MODBUS-MQTT.md
%doc docs/PRIVACY.md docs/REVERSE-PROXY.md docs/SSO.md
%{_bindir}/ser2netweb
%{python3_sitelib}/ser2netweb
%{python3_sitelib}/ser2netweb-%{version}.dist-info
%{_unitdir}/ser2netweb.service
%{_sysusersdir}/ser2netweb.conf
%{_tmpfilesdir}/ser2netweb.conf
%attr(0700,ser2netweb,ser2netweb) %dir %{_sharedstatedir}/ser2netweb

%changelog
