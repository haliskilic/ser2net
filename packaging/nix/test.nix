{ lib, pkgs, ... }:

{
  name = "ser2netweb";
  meta.maintainers = [ ];

  nodes.machine = {
    services.ser2netweb.enable = true;
    environment.systemPackages = [ pkgs.curl ];
  };

  testScript = ''
    machine.wait_for_unit("ser2netweb.service")
    machine.wait_for_open_port(8080)
    machine.succeed("curl --fail --silent http://127.0.0.1:8080/healthz")
    machine.succeed("ser2netweb --version | grep -F 'ser2netweb ${pkgs.ser2netweb.version}'")
    machine.succeed("stat -c %U:%a /var/lib/ser2netweb | grep -Fx ser2netweb:700")

    # the first admin without the browser, as the service user
    machine.succeed(
        "echo 'correct horse battery staple' | sudo -u ser2netweb "
        "ser2netweb --data-dir /var/lib/ser2netweb --set-password admin"
    )

    machine.succeed("systemctl reload ser2netweb.service")
    machine.sleep(2)
    machine.succeed("systemctl is-active ser2netweb.service")
    machine.succeed("curl --fail --silent http://127.0.0.1:8080/healthz")
  '';
}
