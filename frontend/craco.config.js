// craco.config.js
const path = require("path");
require("dotenv").config();

let webpackConfig = {
  eslint: {
    configure: {
      extends: ["plugin:react-hooks/recommended"],
      rules: {
        "react-hooks/rules-of-hooks": "error",
        "react-hooks/exhaustive-deps": "warn",
      },
    },
  },
  webpack: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    configure: (webpackConfig) => {
      // The billing route is TypeScript while the app entry is JavaScript.
      // CRA's default resolver here omitted .ts/.tsx, making production builds
      // fail before the application could load.
      webpackConfig.resolve.extensions = [".js", ".jsx", ".ts", ".tsx", ".json"];

      // Add ignored patterns to reduce watched directories
        webpackConfig.watchOptions = {
          ...webpackConfig.watchOptions,
          ignored: [
            '**/node_modules/**',
            '**/.git/**',
            '**/build/**',
            '**/dist/**',
            '**/coverage/**',
            '**/public/**',
        ],
      };

      return webpackConfig;
    },
  },
};

module.exports = webpackConfig;
