const createApp = require('./src/app');
const config = require('./config');

const app = createApp();

app.listen(config.port, () => {
  console.log(`Server listening on port ${config.port} [${config.env}]`);
});
