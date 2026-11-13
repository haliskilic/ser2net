{
  config,
  lib,
  pkgs,
  utils,
  ...
}:

let
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    types
    ;

  cfg = config.services.ser2netweb;

  stateDir = "/var/lib/ser2netweb";
in
{
  meta.maintainers = [ ];

  options.services.ser2netweb = {
    enable = mkEnableOption "ser2netweb, a serial-to-network bridge with a web UI";

    package = lib.mkPackageOption pkgs "ser2netweb" { };

    listenAddress = mkOption {
      type = types.nullOr types.str;
      default = "127.0.0.1";
      example = "*";
      description = ''
        The address of the web UI and the REST API (`*`: every interface, IPv4 and IPv6).
        `null` leaves it to {file}`config.json`, where the web UI's settings keep it.
      '';
    };

    port = mkOption {
      type = types.nullOr types.port;
      default = 8080;
      description = ''
        The port of the web UI and the REST API. `null` leaves it to {file}`config.json`.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Whether to open {option}`services.ser2netweb.port` in the firewall. The ports of
        the mappings (configured in the web UI) are opened with
        {option}`networking.firewall.allowedTCPPorts`.
      '';
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Extra command line arguments for {command}`ser2netweb`.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.openFirewall -> cfg.port != null;
        message = "services.ser2netweb.openFirewall needs services.ser2netweb.port";
      }
    ];

    # (--set-password, --sign-out-all and --reload run as the service user:
    # sudo -u ser2netweb ser2netweb --data-dir /var/lib/ser2netweb --set-password admin)
    environment.systemPackages = [ cfg.package ];

    networking.firewall.allowedTCPPorts = mkIf cfg.openFirewall [ cfg.port ];

    users.users.ser2netweb = {
      isSystemUser = true;
      group = "ser2netweb";
      home = stateDir;
      description = "ser2netweb serial-to-network bridge";
    };
    users.groups.ser2netweb = { };

    systemd.services.ser2netweb = {
      description = "ser2netweb serial-to-network bridge";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      environment =
        lib.optionalAttrs (cfg.listenAddress != null) { SER2NETWEB_BIND_IP = cfg.listenAddress; }
        // lib.optionalAttrs (cfg.port != null) { SER2NETWEB_PORT = toString cfg.port; };

      serviceConfig = {
        ExecStart = utils.escapeSystemdExecArgs (
          [
            (lib.getExe cfg.package)
            "--data-dir"
            stateDir
          ]
          ++ cfg.extraArgs
        );
        # read config.json again without a restart
        ExecReload = "${lib.getExe' pkgs.coreutils "kill"} -HUP $MAINPID";
        User = "ser2netweb";
        Group = "ser2netweb";
        # the serial ports
        SupplementaryGroups = [ "dialout" ];
        StateDirectory = "ser2netweb";
        StateDirectoryMode = "0700";
        Restart = "on-failure";
        RestartSec = 3;
        # about three per running mapping (port, listener, log) and one per client
        LimitNOFILE = 65536;

        # Hardening
        CapabilityBoundingSet = "";
        LockPersonality = true;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProcSubset = "pid";
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RemoveIPC = true;
        # AF_NETLINK: the list of network interfaces
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_NETLINK"
          "AF_UNIX"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallErrorNumber = "EPERM";
        SystemCallFilter = [
          "@system-service"
          "~@privileged @resources"
        ];
        UMask = "0077";
      };
    };
  };
}
