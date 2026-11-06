// pm2 start examples/deploy/ecosystem.config.js && pm2 save
// 用 pm2 常驻：崩了自己拉起来，开机自启再加一句 pm2 startup
module.exports = {
  apps: [{
    name: 'anchor-memory',
    script: './examples/deploy/start.sh',
    cwd: __dirname + '/../..',
    interpreter: 'bash',
    autorestart: true,
    max_memory_restart: '1500M',
  }],
};
