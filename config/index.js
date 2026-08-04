const environments = {
  development: {
    port: process.env.PORT || 3000,
    logLevel: 'dev',
  },
  test: {
    port: process.env.PORT || 0,
    logLevel: 'silent',
  },
  production: {
    port: process.env.PORT || 8080,
    logLevel: 'combined',
  },
};

const env = process.env.NODE_ENV || 'development';

if (!environments[env]) {
  throw new Error(`Unknown NODE_ENV "${env}". Expected one of: ${Object.keys(environments).join(', ')}`);
}

module.exports = {
  env,
  ...environments[env],
};
