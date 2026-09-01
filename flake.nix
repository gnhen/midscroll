{
  description = "nix flake for midscroll";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          inherit (pkgs) lib;

          pythonEnv = pkgs.python3.withPackages (ps: [
            ps.evdev
            ps.pygobject3
            ps.pycairo
          ]);

          midscroll = pkgs.stdenv.mkDerivation (_finalAttrs: {
            pname = "midscroll";
            version = "1.14"; # latest release version when writing this flake

            src = ./.;

            nativeBuildInputs = [
              pkgs.makeWrapper
              pkgs.wrapGAppsHook4
              pkgs.gobject-introspection
            ];

            buildInputs = [
              pkgs.gtk4
              pkgs.gtk4-layer-shell
              pkgs.librsvg
              pkgs.hicolor-icon-theme
            ];

            dontConfigure = true;
            dontBuild = true;

            installPhase = ''
              runHook preInstall

              install -Dm755 midscroll.py         $out/bin/midscroll
              install -Dm755 midscroll-overlay.py  $out/bin/midscroll-overlay
              install -Dm755 midscroll-settings.py $out/bin/midscroll-settings
              install -Dm755 midscroll-apply.py    $out/bin/midscroll-apply

              install -Dm644 io.github.gnhen.midscroll.Settings.desktop \
                $out/share/applications/io.github.gnhen.midscroll.Settings.desktop
              install -Dm644 io.github.gnhen.midscroll.Settings.metainfo.xml \
                $out/share/metainfo/io.github.gnhen.midscroll.Settings.metainfo.xml
              install -Dm644 io.github.gnhen.midscroll.policy \
                $out/share/polkit-1/actions/io.github.gnhen.midscroll.policy

              install -Dm644 icons/move-vertical.svg $out/share/midscroll/move-vertical.svg
              install -Dm644 icons/move-vertical.svg \
                $out/share/icons/hicolor/scalable/apps/midscroll.svg

              install -Dm644 midscroll.conf $out/share/midscroll/midscroll.conf

              runHook postInstall
            '';

            dontWrapGApps = true;

            postInstall = ''
              export GDK_PIXBUF_MODULE_FILE="${
                pkgs.gnome._gdkPixbufCacheBuilder_DO_NOT_USE { extraLoaders = [ pkgs.librsvg ]; }
              }"
            '';

            postFixup = ''
              for f in midscroll midscroll-overlay midscroll-settings midscroll-apply; do
                sed -i "1s|^#!.*|#!${pythonEnv}/bin/python3|" "$out/bin/$f"
              done

              substituteInPlace "$out/share/polkit-1/actions/io.github.gnhen.midscroll.policy" \
                --replace-fail "/usr/bin/midscroll-apply" "$out/bin/midscroll-apply"
              substituteInPlace "$out/bin/midscroll-settings" \
                --replace-fail "/usr/bin/midscroll-apply" "$out/bin/midscroll-apply"

              wrapProgram "$out/bin/midscroll-apply" \
                --prefix PATH : ${lib.makeBinPath [ pkgs.systemd ]}

              # below is midscroll-overlay related stuff
              # its is important to know that it only works on KDE
              # with kdotool, which is also mentioned on the github readme
              # so there is also option to enable disable its service
              # `services.midscroll.overlay.enable = false;`
              layerShellLib=$(find ${pkgs.gtk4-layer-shell}/lib -maxdepth 1 -name 'libgtk4-layer-shell.so*' | sort | head -n1)
              wrapProgram "$out/bin/midscroll-overlay" \
                "''${gappsWrapperArgs[@]}" \
                --prefix PATH : ${lib.makeBinPath [ pkgs.kdotool pkgs.xprop ]} \
                --set-default LD_PRELOAD "$layerShellLib"

              wrapProgram "$out/bin/midscroll-settings" \
                "''${gappsWrapperArgs[@]}"
            '';

            meta = {
              description = "FOSS Middle Mouse Scroll replacement for Linux";
              homepage = "https://github.com/gnhen/midscroll";
              license = lib.licenses.unlicense;
              platforms = lib.platforms.linux;
              mainProgram = "midscroll";
            };
          });
        in
        {
          inherit midscroll;
          default = midscroll;
        }
      );

      overlays.default = final: _prev: {
        midscroll = self.packages.${final.stdenv.hostPlatform.system}.midscroll;
      };

      nixosModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.services.midscroll;

          formatValue =
            v:
            if builtins.isBool v then
              (if v then "true" else "false")
            else if builtins.isList v then
              lib.concatStringsSep ", " (map toString v)
            else
              toString v;

          confText =
            lib.concatStringsSep "\n" (lib.mapAttrsToList (k: v: "${k} = ${formatValue v}") cfg.settings)
            + "\n";
        in
        {
          options.services.midscroll = {
            enable = lib.mkEnableOption "midscroll, FOSS Middle Mouse Scroll replacement for Linux";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.midscroll;
              defaultText = lib.literalExpression "midscroll.packages.<system>.default";
              description = "The midscroll package to use";
            };

            settings = lib.mkOption {
              type =
                with lib.types;
                attrsOf (oneOf [
                  str
                  int
                  float
                  bool
                  (listOf str)
                ]);
              default = { };
              example = {
                SPEED_MULT = 0.01;
                NATURAL = true;
                # those apps have native middle scroll on linux
                # blacklisting them makes sense.
                # those programs are blacklisted by default as author suggests it
                BLACKLIST = [
                  "freecad"
                  "orcaslicer"
                  "minecraft"
                ];
              };
              description = ''
                Settings rendered into /etc/midscroll.conf, in midscroll's
                own `KEY = value` format (see the upstream README's Tuning
                section for the full list of options).
              '';
            };

            overlay.enable = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = ''
                Enabling per-user service, midscroll-overlay.
                It makes possible to to see scroll badge and ghost cursor.
                Main functionality and systemd daemon works without this,
                but visual feedback will be lacking.
                Also, its important to know that as for now this only works with KDE using kdotool.
                other wayland compositors are not compatible. Can't say much about xorg
              '';
            };
          };

          config = lib.mkIf cfg.enable {
            boot.kernelModules = [ "uinput" ];

            environment.systemPackages = [ cfg.package ];

            environment.etc."midscroll.conf" = lib.mkIf (cfg.settings != { }) {
              text = confText;
            };

            systemd.tmpfiles.rules = lib.mkIf (cfg.settings == { }) [
              "C /etc/midscroll.conf 0644 root root - ${cfg.package}/share/midscroll/midscroll.conf"
            ];

            security.polkit.enable = true;

            systemd.services.midscroll = {
              description = "midscroll - FOSS autoscroll for Linux";
              wants = [ "modprobe@uinput.service" ];
              after = [ "modprobe@uinput.service" ];
              wantedBy = [ "multi-user.target" ];
              serviceConfig = {
                ExecStart = "${cfg.package}/bin/midscroll";
                Restart = "always";
                RestartSec = 2;
                RuntimeDirectory = "midscroll";
                CPUSchedulingPolicy = "fifo";
                CPUSchedulingPriority = 20;

                NoNewPrivileges = true;
                CapabilityBoundingSet = "";
                AmbientCapabilities = "";
                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                PrivateNetwork = true;
                IPAddressDeny = "any";
                RestrictAddressFamilies = "AF_UNIX";
                ProtectProc = "invisible";
                ProcSubset = "pid";
                ProtectKernelTunables = true;
                ProtectKernelLogs = true;
                ProtectKernelModules = true;
                ProtectControlGroups = true;
                ProtectClock = true;
                ProtectHostname = true;
                RestrictNamespaces = true;
                RestrictSUIDSGID = true;
                LockPersonality = true;
                SystemCallArchitectures = "native";
                UMask = "0077";
                MemoryMax = "128M";
                TasksMax = 32;
                SystemCallFilter = "@system-service";
                SystemCallErrorNumber = "EPERM";
                MemoryDenyWriteExecute = true;
              };
            };

            systemd.user.services.midscroll-overlay = lib.mkIf cfg.overlay.enable {
              description = "visual feedback for midscroll";
              after = [ "graphical-session.target" ];
              partOf = [ "graphical-session.target" ];
              wantedBy = [ "graphical-session.target" ];
              serviceConfig = {
                ExecStart = "${cfg.package}/bin/midscroll-overlay";
                Restart = "on-failure";
                RestartSec = 2;

                NoNewPrivileges = true;
                CapabilityBoundingSet = "";
                AmbientCapabilities = "";
                ProtectSystem = "full";
                ProtectKernelTunables = true;
                ProtectKernelLogs = true;
                ProtectKernelModules = true;
                ProtectControlGroups = true;
                ProtectClock = true;
                ProtectHostname = true;
                RestrictNamespaces = true;
                RestrictSUIDSGID = true;
                RestrictRealtime = true;
                LockPersonality = true;
                SystemCallArchitectures = "native";
                RestrictAddressFamilies = "AF_UNIX AF_NETLINK";
                UMask = "0077";
                TasksMax = 64;
                SystemCallFilter = "@system-service";
                SystemCallErrorNumber = "EPERM";
              };
            };
          };
        };

      nixosModules.midscroll = self.nixosModules.default;

      formatter = forAllSystems (system: (import nixpkgs { inherit system; }).nixfmt);
    };
}
