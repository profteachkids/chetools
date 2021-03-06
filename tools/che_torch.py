import re
import requests
import string
from collections import deque
import numpy as np
from itertools import combinations
import torch as torch
from time import time
import math

torch.set_default_tensor_type(torch.DoubleTensor)
torch.set_default_dtype(torch.float64)

two_pi=2*np.pi
one_third = 1/3

def timing(f):
    @wraps(f)
    def wrap(*args, **kw):
        ts = time()
        result = f(*args, **kw)
        te = time()
        print (f'{f.__name__} {args} {kw} {te-ts:2.4}')
        return result
    return wrap


extract_single_props = {'Molecular Weight' : 'Mw',
                        'Critical Temperature' : 'Tc',
                        'Critical Pressure' : 'Pc',
                        'Critical Volume' : 'Vc',
                        'Acentric factor' : 'w',
                        'Normal boiling point' : 'Tb',
                        'IG heat of formation' : 'HfIG',
                        'IG Gibbs of formation' : 'GfIG',
                        'Heat of vaporization' : 'HvapNB'}

extract_coeff_props={'Vapor Pressure' : 'Pvap',
                     'Ideal Gas Heat Capacity':'CpIG',
                     'Liquid Heat Capacity' : 'CpL',
                     'Solid Heat Capacity' : 'CpS',
                     'Heat of Vaporization' : 'Hvap',
                     'Liquid Density' : 'rhoL'}

extract_poly_coeff_props={'Polynomial Ideal Gas Heat Capacity (cal/mol-K)':'polyCpIG'}

base_url = 'https://raw.githubusercontent.com/profteachkids/CHE2064/master/data/'

BIP_file = 'https://raw.githubusercontent.com/profteachkids/CHE2064/master/data/BinaryNRTL.txt'

class Props():
    def __init__(self,comps, get_NRTL=True, base_url=base_url, extract_single_props=extract_single_props,
                 extract_coeff_props=extract_coeff_props, BIP_file=BIP_file, suffix='Props.txt'):

        comps = [comps] if isinstance(comps,str) else comps
        self.N_comps = len(comps)

        id_pat = re.compile(r'ID\s+(\d+)')
        formula_pat = re.compile(r'Formula:\s+([A-Z0-9]+)')
        single_props_pat = re.compile(r'^\s+([\w \/.]+?)\s+:\s+([-.0-9e+]+) +([-\w/.()*]*) *$', re.MULTILINE)
        coeffs_name_pat = re.compile(r"([\w ]+)\s[^\n]*?Equation.*?Coeffs:([- e\d.+]+)+?", re.DOTALL)
        coeffs_pat = re.compile(r'([-\de.+]+)')
        poly_coeffs_pat = re.compile(r"([- \/'()A-Za-z]*)\n Coefficients: +([-+e\d.+]*)\n* *([-+e\d.+]*)\n* *([-+e\d.+]*)\n* *([-+e\d.+]*)\n* *([-+e\d.+]*)\n* *([-+e\d.+]*)\n* *([-+e\d.+]*)")

        props_deque=deque()
        for comp in comps:
            res = requests.get(base_url+comp + suffix)
            if res.status_code != 200:
                raise ValueError(f'{comp} - no available data')
            text=res.text
            props={'Name': comp}
            units={}
            props['ID']=id_pat.search(text).groups(1)[0]
            props['Formula']=formula_pat.search(text).groups(1)[0]
            single_props = dict((item[0], item[1:]) for item in single_props_pat.findall(text))
            for k,v in extract_single_props.items():
                props[v]=float(single_props[k][0])
                units[v]=single_props[k][1]
                props[v] = props[v]*2.20462*1055.6 if units[v]=='Btu/lbmol' else props[v]
                props[v] = props[v]*6894.76 if units[v]=='psia' else props[v]
                props[v] = (props[v]-32)*5/9 + 273.15 if units[v] =='F' else props[v]

            coeffs_name_strings = dict(coeffs_name_pat.findall(text))
            for k,v in extract_coeff_props.items():
                coeffs = coeffs_pat.findall(coeffs_name_strings[k])
                for letter, value in zip(string.ascii_uppercase,coeffs):
                    props[v+letter]=float(value)
            poly_props = dict([(item[0], item[1:]) for item in poly_coeffs_pat.findall(text)])
            for k,v in extract_poly_coeff_props.items():
                for letter, value in zip(string.ascii_uppercase,poly_props[k]):
                    if value == '':
                        break
                    props[v+letter]=float(value)


            props_deque.append(props)

        for prop in props_deque[0].keys():
            if self.N_comps>1:
                values = np.array([comp[prop] for comp in props_deque])
            else:
                values = props_deque[0][prop]
            if values.dtype==np.float64:
                values=torch.from_numpy(values)
            setattr(self,prop,values)


        # kmol to mol
        self.Vc = self.Vc/1000.
        self.HfIG = self.HfIG/1000.
        self.HfL = self.HfIG - self.Hvap(298.15)
        self.GfIG = self.GfIG/1000.

        if (self.N_comps > 1) and get_NRTL:
            text = requests.get(BIP_file).text

            comps_string = '|'.join(self.ID)
            id_name_pat = re.compile(r'^\s+(\d+)[ ]+(' + comps_string +')[ ]+[A-Za-z]',re.MULTILINE)
            id_str = id_name_pat.findall(text)
            #maintain order of components
            id_dict = {v:k for k,v in id_str}

            # list of comp IDs with BIP data
            id_str = [id_dict.get(id, None) for id in self.ID]
            id_str = list(filter(None, id_str))
            comb_strs = combinations(id_str,2)
            comb_indices = combinations(range(self.N_comps),2)
            self.NRTL_A, self.NRTL_B, self.NRTL_C, self.NRTL_D, self.NRTL_alpha = np.zeros((5, self.N_comps,self.N_comps))
            start=re.search(r'Dij\s+Dji',text).span()[0]

            for comb_str, comb_index in zip(comb_strs, comb_indices):
                comb_str = '|'.join(comb_str)
                comb_values_pat = re.compile(r'^[ ]+(' + comb_str +
                                             r')[ ]+(?:' + comb_str + r')(.*)$', re.MULTILINE)


                match = comb_values_pat.search(text[start:])
                if match is not None:
                    first_id, values = match.groups(1)
                    #if matched order is flipped, also flip indices
                    if first_id != comb_index[0]:
                        comb_index = (comb_index[1],comb_index[0])
                    bij, bji, alpha, aij, aji, cij, cji, dij, dji  = [float(val) for val in values.split()]
                    np.add.at(self.NRTL_B, comb_index, bij)
                    np.add.at(self.NRTL_B, (comb_index[1],comb_index[0]), bji)
                    np.add.at(self.NRTL_A, comb_index, aij)
                    np.add.at(self.NRTL_A, (comb_index[1],comb_index[0]), aji)
                    np.add.at(self.NRTL_C, comb_index, cij)
                    np.add.at(self.NRTL_C, (comb_index[1],comb_index[0]), cji)
                    np.add.at(self.NRTL_D, comb_index, dij)
                    np.add.at(self.NRTL_D, (comb_index[1],comb_index[0]), dji)
                    np.add.at(self.NRTL_alpha, comb_index, alpha)
                    np.add.at(self.NRTL_alpha, (comb_index[1],comb_index[0]), alpha)

            self.NRTL_A = torch.from_numpy(self.NRTL_A)
            self.NRTL_B = torch.from_numpy(self.NRTL_B)
            self.NRTL_C = torch.from_numpy(self.NRTL_C)
            self.NRTL_D = torch.from_numpy(self.NRTL_D)
            self.NRTL_alpha = torch.from_numpy(self.NRTL_alpha)


    def Pvap(self,T):
        return torch.exp(self.PvapA + self.PvapB/T + self.PvapC*math.log(T) +
                       self.PvapD*torch.pow(T,self.PvapE))

    def CpIG(self, T):
        return (self.CpIGA + self.CpIGB*(self.CpIGC/T/torch.sinh(self.CpIGC/T))**2 +
                self.CpIGD*(self.CpIGE/T/torch.cosh(self.CpIGE/T))**2)

    def deltaHsensIGpoly(self, T):
        return T * (self.polyCpIGA + T * (self.polyCpIGB / 2 + T * (self.polyCpIGC / 3 + T * (self.polyCpIGD / 4 + T* (self.polyCpIGE / 5 + T*self.polyCpIGF/6)))))*4.184

    def HIGpoly(self, nV, T):
        return torch.dot(nV, self.HfIG + self.deltaHsensIGpoly(T) - self.deltaHsensIGpoly(298.15))

    def deltaHsensIG(self, T):
        return (self.CpIGA*T + self.CpIGB * self.CpIGC/torch.tanh(self.CpIGC/T) - self.CpIGD * self.CpIGE * torch.tanh(self.CpIGE/T))/1000

    def HIG(self, nV, T):
        return torch.dot(nV, self.HfIG + self.deltaHsensIG(T) - self.deltaHsensIG(298.15))

    def Hvap(self, T):
        Tr = T/self.Tc
        return (self.HvapA*torch.pow(1-Tr, self.HvapB + (self.HvapC+(self.HvapD+self.HvapE*Tr)*Tr)*Tr ))/1000.

    def deltaHsensL(self, T):
        return T * (self.CpLA + T * (self.CpLB / 2 + T * (self.CpLC / 3 + T * (self.CpLD / 4 + self.CpLE / 5 * T))))/1000.

    def Hv(self, nV, T):
        return self.Hl(nV, T) + torch.dot(nV, self.Hvap(T))

    def Hl(self, nL, T):
        return torch.dot(nL, self.HfL + self.deltaHsensL(T) - self.deltaHsensL(298.15))

    def rhol(self, T):
        return(self.rhoLA / torch.pow(self.rhoLB, 1+ torch.pow((1.-T/self.rhoLC),self.rhoLD)) *self.Mw)

    def NRTL_gamma(self, x, T):
        tau = (self.NRTL_A + self.NRTL_B / T + self.NRTL_C * math.log(T) +
               self.NRTL_D * T)
        G = torch.exp(-self.NRTL_alpha * tau)

        xG=torch.matmul(x,G)
        xtauGdivxG = torch.matmul(x,tau * G) / xG
        lngamma = xtauGdivxG + torch.matmul((x / xG), (G * (tau - xtauGdivxG)).T)
        return torch.exp(lngamma)

    def Gex(self, x,T):
        tau = (self.NRTL_A + self.NRTL_B / T + self.NRTL_C * math.log(T) +
               self.NRTL_D * T)
        G = torch.exp(-self.NRTL_alpha * tau)
        xG=torch.matmul(x,G)
        xtauGdivxG = torch.matmul(x,(tau * G)) / xG
        return torch.dot(x, xtauGdivxG)

    def NRTL_gamma2(self,x, T):
        x.requires_grad_(True)
        Gex = self.Gex(x,T)
        return torch.exp(torch.autograd.grad([Gex],x)[0])


class Comp:
    
    def __init__(self,x=None,q=None):
        if q is None:
            self.x=x
        else:
            self.q=q


    @property
    def x(self):
        return self.x_
    
    @property
    def q(self):
        return self.q_

    @x.setter
    def x(self,v):
        v=torch.as_tensor(v)
        self.x_=v/v.sum()
        self.q_=torch.log(v[:-1]) + torch.log(1.+ (1. - v[-1])/v[-1])

    @q.setter
    def q(self,q):
        q=torch.as_tensor(q)
        self.q_=q
        xm1 = torch.exp(q)/(1+torch.sum(torch.exp(q)))
        self.x_=torch.cat((xm1, torch.atleast_1d(1.-torch.sum(xm1))))




def qtox(q):
    q=torch.atleast_1d(q)
    xm1 = torch.exp(q)/(1+torch.sum(torch.exp(q)))
    return torch.concatenate((xm1, torch.atleast_1d(1.-torch.sum(xm1))))

def xtoq(x):
    x=torch.atleast_1d(x)
    return torch.log(x[:-1]) + torch.log(1.+ (1. - x[-1])/x[-1])


def cubic_roots(a, b, c):
    # Returns only the real roots of cubic equations with real coefficients
    # x**3 + a x**2 + b x + c = 0

    Q = (a * a - 3 * b) / 9
    R = (2 * a * a * a - 9 * a * b + 27 * c) / 54
    det = (R * R - Q ** 3)

    if det<0:
        theta = torch.arccos(R / pow(Q, 1.5))
        x=torch.tensor((torch.cos(theta/3), torch.cos((theta+two_pi)/3), torch.cos((theta-two_pi)/3)))
        x = -2 * torch.sqrt(Q)*x - a/3
        return x
    else:
        A = -torch.sign(R) * (abs(R) + torch.sqrt(det)) ** one_third
        B = Q / A
        return torch.tensor([(A + B) - a / 3, torch.nan, torch.nan])

