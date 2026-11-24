const router = require('express').Router();
const Joi = require('joi');

const zoneSchema = Joi.object({
  name: Joi.string().trim().min(1).max(80).required(),
  speedLimit: Joi.number().integer().min(0).max(130).required(),
});

router.get('/api/incidents', (req, res) => res.json(require('../data/incidents.json')));
router.post('/api/zones', (req, res) => {
  const { error } = zoneSchema.validate(req.body);
  if (error) return res.status(400).json({ error: error.message });
  res.status(201).json({ ok: true });
});
module.exports = router;
