{
  nixConfig = {
    extra-substituters = [
      "https://cache.nixos.org"
      "https://devenv.cachix.org"
      "https://nix-community.cachix.org"
      "https://install.determinate.systems"

    ];
    extra-trusted-substituters = [
      "https://cache.nixos.org"
      "https://devenv.cachix.org"
      "https://nix-community.cachix.org"
      "https://install.determinate.systems"

    ];
    extra-trusted-public-keys = [
      "cache.flakehub.com-3:hJuILl5sVK4iKm86JzgdXW12Y2Hwd5G07qKtHTOcDCM="
    ];
  };
  inputs = {
    systems.url = "github:nix-systems/default";
    nixpkgs.url = "nixpkgs/nixos-unstable";
    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
    lasm = {
      url = "github:DDoSolitary/ld-audit-search-mod";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix_hammer_overrides = {
      url = "github:TyberiusPrime/uv2nix_hammer_overrides";
      inputs.nixpkgs.follows = "nixpkgs";
    };

  };
  outputs =
    inputs:
    inputs.flake-parts.lib.mkFlake { inherit inputs; } (
      { lib, ... }:
      {
        systems = import inputs.systems;
        # imports = [
        #   inputs.nix-ml-ops.flakeModules.devcontainer
        #   inputs.nix-ml-ops.flakeModules.nixIde
        #   inputs.nix-ml-ops.flakeModules.nixLd
        #   inputs.nix-ml-ops.flakeModules.pythonVscode
        #   inputs.nix-ml-ops.flakeModules.ldFallbackManylinux
        #   inputs.nix-ml-ops.flakeModules.devcontainerNix
        # ];
        perSystem =
          perSystem@{
            pkgs,
            system,
            inputs',
            ...
          }:
          let

            workspace = inputs.uv2nix.lib.workspace.loadWorkspace {
              workspaceRoot = ./.;
              # workspaceRoot = lib.fileset.toSource {
              #   root = ./.;
              #   fileset = lib.fileset.difference ./. ./flake.nix;
              # };
            };

            pyprojectOverrides =
              # See https://pyproject-nix.github.io/uv2nix/patterns/overriding-build-systems.html
              final: prev:
              builtins.mapAttrs
                (
                  name: spec:
                  prev.${name}.overrideAttrs (old: {
                    nativeBuildInputs = old.nativeBuildInputs ++ final.resolveBuildSystem spec;
                  })
                )
                {
                  dropboxdrivefs.setuptools = [ ];
                };
            python = pkgs.python3;
            pythonSet =
              (pkgs.callPackage inputs.pyproject-nix.build.packages {
                inherit python;
              }).overrideScope
                (
                  lib.composeManyExtensions [
                    inputs.pyproject-build-systems.overlays.wheel
                    (workspace.mkPyprojectOverlay {
                      sourcePreference = "wheel";
                      dependencies = workspace.deps.default;
                    })
                    (inputs.uv2nix_hammer_overrides.overrides pkgs)
                    pyprojectOverrides
                    (final: prev: {
                      # ratarmount = prev.ratarmount.overrideAttrs (old: {
                      #   propagatedBuildInputs = old.buildInputs or [ ] ++ [
                      #     (lib.getLib pkgs.fuse)
                      #   ];
                      # });
                      indexed-zstd = prev.indexed-zstd.overrideAttrs (old: {
                        nativeBuildInputs =
                          old.nativeBuildInputs
                          ++ final.resolveBuildSystem {
                            setuptools = [ ];
                            cython = [ ];
                          };
                        buildInputs = old.buildInputs or [ ] ++ [
                          pkgs.zstd
                        ];
                      });
                    })
                  ]
                );
            editableOverlay = workspace.mkEditablePyprojectOverlay {
              root = "$REPO_ROOT";
              members = [
                "ratarmountcore"
                "ratarmount"
              ];
            };
            editablePythonSet = pythonSet.overrideScope (
              lib.composeManyExtensions [
                editableOverlay
                pyprojectOverrides
                (
                  final: prev:
                  lib.attrsets.genAttrs
                    [
                      "ratarmountcore"
                      "nativeBuildInputs"
                    ]
                    (
                      name:
                      prev.${name}.overrideAttrs (old: {
                        # It's a good idea to filter the sources going into an editable build
                        # so the editable package doesn't have to be rebuilt on every change.
                        src =
                          let
                            root = (lib.sources.cleanSourceWith { src = old.src; }).origSrc;
                          in
                          lib.fileset.toSource rec {
                            inherit root;
                            fileset = lib.fileset.unions [
                              /${root}/pyproject.toml
                              /${root}/README.md
                              /${root}/${name}/__init__.py
                              /${root}/${name}/version.py
                            ];
                          };

                        # Hatchling (our build system) has a dependency on the `editables` package when building editables.
                        #
                        # In normal Python flows this dependency is dynamically handled, and doesn't need to be explicitly declared.
                        # This behaviour is documented in PEP-660.
                        #
                        # With Nix the dependency needs to be explicitly declared.
                        nativeBuildInputs =
                          old.nativeBuildInputs
                          ++ final.resolveBuildSystem {
                            editables = [ ];
                          };
                      }

                      )
                    )
                )
              ]
            );
            virtualenv = editablePythonSet.mkVirtualEnv "ratarmount-dev-env" workspace.deps.all;
            yamlFormat = pkgs.formats.yaml { };
          in
          {
            packages.default = pythonSet.mkVirtualEnv "ratarmount-env" workspace.deps.default;
            packages.virtualenv = virtualenv;

            devShells.default = pkgs.mkShell {
              packages = [
                virtualenv
                pkgs.nixfmt-rfc-style
                pkgs.shellcheck
                pkgs.uv
                pkgs.pixz
              ];

              env = {
                # Don't create venv using uv
                UV_NO_SYNC = "1";

                # Force uv to use Python interpreter from venv
                UV_PYTHON = "${virtualenv}/bin/python";

                # Prevent uv from downloading managed Python's
                UV_PYTHON_DOWNLOADS = "never";

                LD_LIBRARY_PATH = "${lib.getLib pkgs.fuse}/lib";

                # LD_AUDIT = "${inputs'.lasm.packages.default}/lib/libld-audit-search-mod.so";

                # GLIBC_TUNABLES = "glibc.rtld.optional_static_tls=2000";

                # LD_AUDIT_SEARCH_MOD_CONFIG = toString (
                #   yamlFormat.generate "lasm-config.yaml" {
                #     rules = [
                #       {
                #         cond.rtld = "nix";
                #         default.prepend = [
                #           { dir = "${pkgs.stdenv.cc.cc.lib}/lib"; }
                #         ];
                #       }
                #       {
                #         cond.rtld = "any";
                #         libpath.save = true;
                #         default.prepend = [
                #           { saved = "libpath"; }
                #         ];
                #       }
                #     ];
                #   }
                # );

              };

              shellHook = ''
                # Undo dependency propagation by nixpkgs.
                unset PYTHONPATH

                # Get repository root using git. This is expanded at runtime by the editable `.pth` machinery.
                export REPO_ROOT=$(git rev-parse --show-toplevel)
              '';
            };

          };
      }
    );
}
