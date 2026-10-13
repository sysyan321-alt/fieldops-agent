import client from './client'

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  role: string
  user_id: string
}

export const authApi = {
  login: (data: LoginRequest) =>
    client.post<LoginResponse>('/auth/login', data),
}
