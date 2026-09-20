'use strict';
// The start pipeline, in order. To add a stage, write a Step (see ../pipeline.js) and
// list it here.

module.exports = [
  require('./config'),
  require('./docker'),
  require('./python'),
  require('./ingest'),
  require('./extension'),
  require('./service'),
  require('./open'),
];
