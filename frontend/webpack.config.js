const path = require("path");
const fs = require("fs");
const HtmlWebpackPlugin = require("html-webpack-plugin");
const { container } = require("webpack");
const { createSharedDeps } = require("./moduleFederationShared");

function envInt(name, fallback) {
  const raw = String(process.env[name] ?? "").trim();
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function envText(name, fallback = "") {
  const raw = String(process.env[name] ?? "").trim();
  return raw || fallback;
}

const frontendPort = envInt("TOPOSYNC_FRONTEND_PORT", 5173);
const backendHost = envText("TOPOSYNC_BACKEND_HOST", "127.0.0.1");
const backendPort = envInt("TOPOSYNC_BACKEND_PORT", 8000);
const backendTarget = envText("TOPOSYNC_BACKEND_TARGET", `http://${backendHost}:${backendPort}`);
const brandAssets = [
  "toposync-symbol.svg",
  "toposync-icon-256.png",
  "toposync-icon-512.png",
  "toposync-icon-1024.png",
  "favicon.png"
];

class StaticBrandAssetsPlugin {
  apply(compiler) {
    compiler.hooks.thisCompilation.tap("StaticBrandAssetsPlugin", (compilation) => {
      const { RawSource } = compiler.webpack.sources;
      compilation.hooks.processAssets.tap(
        {
          name: "StaticBrandAssetsPlugin",
          stage: compiler.webpack.Compilation.PROCESS_ASSETS_STAGE_ADDITIONAL
        },
        () => {
          for (const filename of brandAssets) {
            const filePath = path.resolve(__dirname, "src", "assets", filename);
            compilation.emitAsset(filename, new RawSource(fs.readFileSync(filePath)));
          }
        }
      );
    });
  }
}

/** @type {import("webpack").Configuration | ((env: unknown, argv: import("webpack").Configuration) => import("webpack").Configuration)} */
module.exports = (_env, argv = {}) => {
  const isProduction = String(argv.mode || "").trim() === "production";

  return {
    entry: path.resolve(__dirname, "src", "index.tsx"),
    output: {
      path: path.resolve(__dirname, "dist"),
      // In dev, force root-relative assets so client-side routes like /settings/pipelines
      // keep loading /main.js from the dev server root. In production, "auto" is needed
      // so the host works under Home Assistant ingress subpaths.
      publicPath: isProduction ? "auto" : "/",
      clean: true
    },
    resolve: {
      extensions: [".ts", ".tsx", ".js"]
    },
    module: {
      rules: [
        {
          test: /\.tsx?$/,
          loader: "ts-loader",
          options: { transpileOnly: true },
          exclude: /node_modules/
        },
      {
        test: /\.css$/,
        use: ["style-loader", "css-loader"]
      },
      {
        test: /\.(woff2?|eot|ttf|otf|svg)$/i,
        type: "asset/resource"
      },
      {
        test: /\.png$/i,
        type: "asset/resource"
      }
      ]
    },
    plugins: [
      new StaticBrandAssetsPlugin(),
      new HtmlWebpackPlugin({
        template: path.resolve(__dirname, "src", "index.html")
      }),
      new container.ModuleFederationPlugin({
        name: "toposync_host",
        remotes: {},
        shared: createSharedDeps({ includeThree: true })
      })
    ],
    devtool: "source-map",
    devServer: {
      port: frontendPort,
      historyApiFallback: true,
      proxy: [
        {
          context: ["/api", "/extensions", "/files"],
          target: backendTarget,
          changeOrigin: true,
          ws: true
        }
      ]
    }
  };
};
