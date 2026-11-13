# What .github/workflows/distro-packages-2.yml builds against a pinned nixpkgs (not part of a
# nixpkgs submission: there, package.nix is pkgs/by-name/se/ser2netweb/package.nix, module.nix
# nixos/modules/services/networking/ser2netweb.nix and test.nix nixos/tests/ser2netweb.nix):
#
#   nix-build -I nixpkgs=https://github.com/NixOS/nixpkgs/archive/<rev>.tar.gz \
#     packaging/nix/ci.nix -A package     # the package (its --version check runs)
#   nix-build -I nixpkgs=... packaging/nix/ci.nix -A test   # the NixOS VM test (needs KVM)
{
  pkgs ? import <nixpkgs> {
    overlays = [ (final: prev: { ser2netweb = final.callPackage ./package.nix { }; }) ];
  },
}:

{
  package = pkgs.ser2netweb;

  test = pkgs.testers.runNixOSTest {
    imports = [ ./test.nix ];
    defaults.imports = [ ./module.nix ];
  };
}
