import {request,post} from './client';import type {Batch} from '../types/domain';
export interface BatchLimits{max_items:number;max_simulations:number;max_evaluations:number;reference_alpha:number}
export const limits=()=>request<BatchLimits>('/significance/limits');
export const list=()=>request<Batch[]>('/significance');
export const create=(data:unknown)=>post<Batch>('/significance',data);
export const detail=(id:string)=>request<Batch>('/significance/'+id);
export const retry=(id:string)=>post<Batch>('/significance/'+id+'/retry',{});
