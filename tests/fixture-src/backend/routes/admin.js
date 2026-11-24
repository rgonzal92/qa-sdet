const router = require('express').Router();
router.get('/api/admin/stats', (req, res) => res.json({ zones: 0 }));  // no page calls this
module.exports = router;
