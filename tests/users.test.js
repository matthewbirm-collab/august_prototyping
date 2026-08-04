const request = require('supertest');
const createApp = require('../src/app');
const User = require('../src/models/user.model');

const app = createApp();

beforeEach(() => {
  User.reset();
});

describe('GET /health', () => {
  it('returns ok status', async () => {
    const res = await request(app).get('/health');
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ status: 'ok' });
  });
});

describe('POST /api/users', () => {
  it('creates a user with valid data', async () => {
    const res = await request(app)
      .post('/api/users')
      .send({ name: 'Ada Lovelace', email: 'ada@example.com' });

    expect(res.status).toBe(201);
    expect(res.body).toMatchObject({ name: 'Ada Lovelace', email: 'ada@example.com' });
    expect(res.body.id).toBeDefined();
  });

  it('rejects a missing name', async () => {
    const res = await request(app).post('/api/users').send({ email: 'ada@example.com' });
    expect(res.status).toBe(400);
  });

  it('rejects an invalid email', async () => {
    const res = await request(app)
      .post('/api/users')
      .send({ name: 'Ada Lovelace', email: 'not-an-email' });
    expect(res.status).toBe(400);
  });

  it('rejects a duplicate email', async () => {
    await request(app).post('/api/users').send({ name: 'Ada', email: 'dup@example.com' });
    const res = await request(app).post('/api/users').send({ name: 'Bea', email: 'dup@example.com' });
    expect(res.status).toBe(409);
  });
});

describe('GET /api/users', () => {
  it('lists all users', async () => {
    await request(app).post('/api/users').send({ name: 'Ada', email: 'ada@example.com' });
    await request(app).post('/api/users').send({ name: 'Bea', email: 'bea@example.com' });

    const res = await request(app).get('/api/users');
    expect(res.status).toBe(200);
    expect(res.body).toHaveLength(2);
  });
});

describe('GET /api/users/:id', () => {
  it('returns a single user', async () => {
    const created = await request(app).post('/api/users').send({ name: 'Ada', email: 'ada@example.com' });
    const res = await request(app).get(`/api/users/${created.body.id}`);
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ name: 'Ada', email: 'ada@example.com' });
  });

  it('returns 404 for a nonexistent user', async () => {
    const res = await request(app).get('/api/users/999');
    expect(res.status).toBe(404);
  });
});

describe('PUT /api/users/:id', () => {
  it('updates an existing user', async () => {
    const created = await request(app).post('/api/users').send({ name: 'Ada', email: 'ada@example.com' });
    const res = await request(app)
      .put(`/api/users/${created.body.id}`)
      .send({ name: 'Ada Byron' });

    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ name: 'Ada Byron', email: 'ada@example.com' });
  });

  it('returns 404 when updating a nonexistent user', async () => {
    const res = await request(app).put('/api/users/999').send({ name: 'Nobody' });
    expect(res.status).toBe(404);
  });

  it('rejects an invalid email on update', async () => {
    const created = await request(app).post('/api/users').send({ name: 'Ada', email: 'ada@example.com' });
    const res = await request(app)
      .put(`/api/users/${created.body.id}`)
      .send({ email: 'not-an-email' });
    expect(res.status).toBe(400);
  });
});

describe('DELETE /api/users/:id', () => {
  it('deletes an existing user', async () => {
    const created = await request(app).post('/api/users').send({ name: 'Ada', email: 'ada@example.com' });
    const res = await request(app).delete(`/api/users/${created.body.id}`);
    expect(res.status).toBe(204);

    const getRes = await request(app).get(`/api/users/${created.body.id}`);
    expect(getRes.status).toBe(404);
  });

  it('returns 404 when deleting a nonexistent user', async () => {
    const res = await request(app).delete('/api/users/999');
    expect(res.status).toBe(404);
  });
});
