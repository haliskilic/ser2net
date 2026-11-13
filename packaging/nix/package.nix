{
  lib,
  python3Packages,
  fetchFromGitHub,
  versionCheckHook,
  nixosTests,
}:

python3Packages.buildPythonApplication (finalAttrs: {
  pname = "ser2netweb";
  version = "3.0.1";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "haliskilic";
    repo = "ser2netweb";
    tag = "v${finalAttrs.version}";
    hash = "sha256-0i6yjA66dE7Ah75QUjg7EdgQA04IA9/n50YmxHZvGb4=";
  };

  build-system = with python3Packages; [ setuptools ];

  dependencies = with python3Packages; [
    h11
    jinja2
    markupsafe
    psutil
    pyserial
    pyserial-asyncio-fast
    python-multipart
    starlette
    uvicorn
    websockets
  ];

  optional-dependencies = with python3Packages; {
    ldap = [ ldap3 ];
    mqtt = [ paho-mqtt ];
    oidc = [ authlib ];
    tls = [ cryptography ];
  };

  pythonImportsCheck = [
    "ser2netweb"
    "ser2netweb.engine.serial_compat"
    "ser2netweb.web.httpguard"
  ];

  nativeInstallCheckInputs = [ versionCheckHook ];

  passthru.tests = {
    inherit (nixosTests) ser2netweb;
  };

  meta = {
    description = "Serial-to-network bridge with a web UI";
    longDescription = ''
      ser2netweb shares serial ports over the network: raw TCP, telnet, RFC 2217, UDP,
      a Modbus RTU/TCP gateway and MQTT, configured in a web UI or over a REST API.
      ser2netweb is an independent project, unrelated to Corey Minyard's ser2net.
    '';
    homepage = "https://github.com/haliskilic/ser2netweb";
    changelog = "https://github.com/haliskilic/ser2netweb/blob/${finalAttrs.src.tag}/CHANGELOG.md";
    # htmx (0BSD) and xterm.js (MIT) in the web UI; code copied from pyserial-asyncio-fast
    # and Starlette (BSD-3-Clause)
    license = with lib.licenses; [
      agpl3Only
      bsd0
      bsd3
      mit
    ];
    maintainers = [ ];
    mainProgram = "ser2netweb";
  };
})
